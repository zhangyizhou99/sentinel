"""Sentinel 对话式 Web 界面（Gradio ChatInterface）。

用户在对话里下达目标（如「扫一下 /path/to/repo」），交给三范式 AgentCore 自治执行：
    plan → act(ReAct，真实调用 scan 工具) → reflect
配了 LLM key 时是真正的 agent 推理；没配 key 时降级为「纯扫描直报」，仍可用。

运行：PYTHONPATH=src python3 -m sentinel.webapp
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Optional
from urllib.parse import quote

import gradio as gr

# --- Gradio 4.44 已知 bug 兜底 ---------------------------------------------
# gradio_client 生成 API schema 时，遇到布尔型 schema（如 additionalProperties: true）
# 会抛 TypeError: argument of type 'bool' is not iterable，进而让启动自检误报
# 「localhost 不可达」导致退出。打个补丁：布尔 schema 一律当作 Any。
try:  # pragma: no cover - 纯环境兜底
    import gradio_client.utils as _gcu

    _ORIG_J2P = _gcu._json_schema_to_python_type

    def _safe_json_schema_to_python_type(schema, defs=None):
        if isinstance(schema, bool):
            return "Any"
        return _ORIG_J2P(schema, defs)

    _gcu._json_schema_to_python_type = _safe_json_schema_to_python_type
except Exception:  # noqa: BLE001
    pass

from sentinel.config import workspace_root
from sentinel.engines.agent import AgentCore, AgentRun
from sentinel.engines.agent_tools import (
    build_find_repo_tool,
    build_scan_tool,
    build_check_language_tool,
    build_install_language_tool,
    build_feedback_tool,
    build_note_tool,
    build_recall_notes_tool,
)
from sentinel.engines.scan import scan_repo, signals_of
from sentinel.engines.judge import judge_intent
from sentinel.cognition import CodeIndex
from sentinel.llm import LLMClient
from sentinel.permissions import PermissionBroker

# 全局单例：LLM 客户端（无 key 时 available=False，走降级路径，不崩）。
_LLM = LLMClient()

# 情节记忆单例：跨会话/跨接口共享（CLI 标的忽略，Web 扫描也会抑制）。
_MEMORY = None


def _get_memory():
    global _MEMORY
    if _MEMORY is None:
        from sentinel.memory import EpisodicMemory
        _MEMORY = EpisodicMemory()
    return _MEMORY

# 笔记库单例（团队笔记 → 判定上下文的一等证据）。
_NOTES = None


def _get_notes():
    global _NOTES
    if _NOTES is None:
        from sentinel.memory import NoteStore
        _NOTES = NoteStore()
    return _NOTES

# 意图判定用的向量索引 embedder 单例（避免每条消息重载模型）。
_EMBEDDER = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentinel.cognition import FastEmbedEmbedder
        _EMBEDDER = FastEmbedEmbedder()
    return _EMBEDDER

# 已授权可「打开文件」的路径集合（服务端级）。与 scan 授权一致：只有用户
# 同意扫描过（或降级模式下显式发起扫描）的目录，其文件才允许被 /open 打开。
_AUTHORIZED_PATHS: set = set()


def _authorize_open(path: str) -> None:
    """登记一个已授权目录（其下文件之后可被 /open 打开）。"""
    _AUTHORIZED_PATHS.add(os.path.abspath(path))


def _is_open_authorized(ap: str) -> bool:
    return any(ap == g or ap.startswith(g + os.sep) for g in _AUTHORIZED_PATHS)


def _build_agent(broker: PermissionBroker) -> AgentCore:
    """构造 agent 工具集：查找/扫描/语言检测·补齐/反馈/笔记（均受该会话权限门约束）。"""
    mem = _get_memory()
    notes = _get_notes()
    find = build_find_repo_tool(broker)
    scan = build_scan_tool(broker, memory=mem, notes=notes)  # 带记忆：抑制被拒的 + 记录运行 + 学埋点约定
    check = build_check_language_tool(broker)             # 只读：报告语言覆盖缺口
    install = build_install_language_tool(_LLM)           # 破坏性：须用户明确同意后才补齐
    ignore = build_feedback_tool(mem)                     # 反馈学习：标为不用埋点→下次抑制
    add_note = build_note_tool(notes, mem)               # 记团队笔记 → 判定上下文一等证据
    recall = build_recall_notes_tool(notes, mem)         # 查团队笔记
    tools = {t.name: t for t in (find, scan, check, install, ignore, add_note, recall)}
    return AgentCore(_LLM, tools=tools)


# -- 回复格式化 ------------------------------------------------------------

def _scan_reports(run: AgentRun) -> List[dict]:
    """从 transcript 里取出 scan 工具的真实观测（ground truth，不用 LLM 改写过的答案）。"""
    reports: List[dict] = []
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") == "scan":
            obs = item.get("observation", {})
            if isinstance(obs.get("result"), dict):
                reports.append(obs["result"])
    return reports


def _open_link(repo: str, rel_file: str, lines: str) -> str:
    """拼一个指向本机服务 /open 端点的相对链接。

    为什么不用 vscode:// ：Gradio 前端会把非 http 的自定义协议链接过滤掉（点不动）。
    改成同源 http 链接就不会被清洗；点击时服务端用 code -g 打开（见 /open 路由）。
    """
    abspath = rel_file if os.path.isabs(rel_file) else os.path.join(repo or "", rel_file)
    start = str(lines or "1").split("-")[0].strip() or "1"
    return f"/open?path={quote(abspath)}&line={start}"


def _open_in_editor(path: str, line: int) -> None:
    """服务端在本机用 VS Code 打开文件到指定行。

    双重门（§14）：既要在工作区范围内，又要该目录**已被授权**（与 scan 一致）——
    没授权过的仓库文件一律不开，即使有人构造链接也没用。
    """
    root = workspace_root()
    ap = os.path.abspath(path)
    in_scope = ap == root or ap.startswith(root + os.sep)
    if not (in_scope and _is_open_authorized(ap)) or not os.path.exists(ap):
        return  # 越界 / 未授权 / 不存在 → 忽略，不打开
    code = shutil.which("code")
    try:
        if code:  # 有 code CLI 最直接
            subprocess.run([code, "-g", f"{ap}:{line}"], check=False)
        else:      # 否则 macOS 用 open 把 vscode:// URI 交给系统路由到 VS Code（无需 code 命令）
            subprocess.run(["open", f"vscode://file{ap}:{line}"], check=False)
    except Exception:  # noqa: BLE001
        pass


def _language_gap_note(data: dict) -> str:
    """语言缺口提示：有文件因未装解析器被静默跳过时，显式告诉用户（别让人以为扫全了）。"""
    gap = data.get("language_gap") or {}
    if not gap:
        return ""
    items = "、".join(f"{lang}({n} 个文件)" for lang, n in gap.items())
    return (f"\n\n⚠️ **另有未扫描的文件**：{items} —— 因为还没安装对应语言的解析器，"
            f"这些文件被跳过、**不在上面的盲区统计里**。"
            f"回复「装上 <语言>」或直接说「补齐」，我会在你同意后安装并重新扫描。")


def _render_report(data: dict) -> str:
    """把一份 scan 报告渲染成易读表格；文件名做成可点击的 VS Code 深链。"""
    spots = data.get("blind_spots", [])
    repo = data.get("repo", "")
    suppressed = data.get("suppressed_count", 0)
    supp_note = f"（已抑制 **{suppressed}** 个你此前标记忽略的函数）" if suppressed else ""
    head = (f"扫描 `{repo or '?'}`：共 **{data.get('total_units', '?')}** 个函数，"
            f"发现 **{data.get('blind_spot_count', len(spots))}** 个监控盲区{supp_note}。")
    gap_note = _language_gap_note(data)
    if not spots:
        return head + "\n\n✅ 没有明显盲区。" + gap_note
    rows = ["| 文件（点击在 VS Code 打开） | 函数 | 风险信号 | 行 |", "| --- | --- | --- | --- |"]
    for b in spots:
        sig = ", ".join(b.get("signals", [])) or "-"
        link = _open_link(repo, b.get("file", ""), b.get("lines", ""))
        file_cell = f"[{b.get('file', '?')}]({link})"
        rows.append(f"| {file_cell} | `{b.get('function', '?')}` | {sig} | {b.get('lines', '?')} |")
    return head + "\n\n" + "\n".join(rows) + gap_note


def _clip(text, n: int = 180) -> str:
    """截断长文本，便于在轨迹里展示。"""
    text = str(text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _summarize_obs(obs) -> str:
    """把一次工具观测压成一句人话（便于学习，不刷屏）。"""
    if not isinstance(obs, dict):
        return _clip(obs)
    if "error" in obs:
        return f"⚠️ {_clip(obs['error'])}"
    res = obs.get("result", obs)
    if isinstance(res, dict):
        if "blind_spots" in res:
            return (f"扫描 → 共 {res.get('total_units', '?')} 个函数，"
                    f"{res.get('blind_spot_count', '?')} 个盲区")
        if "matches" in res:
            ms = res.get("matches", [])
            names = ", ".join(os.path.basename(m) for m in ms[:3])
            return f"找到 {len(ms)} 个匹配" + (f"：{names}" if names else "")
        if res.get("permission_required"):
            return f"🔐 需授权：{res['permission_required']}"
        if res.get("denied"):
            return "⛔ 越界，拒绝访问"
    return _clip(res)


def _format_trace(run: AgentRun) -> str:
    """把一次自治运行渲染成「三范式」可读轨迹，供学习：Plan → ReAct → Reflection。"""
    L: List[str] = []

    # ① Plan-and-Execute：动手前先把目标拆成有序计划。
    if run.plan:
        L.append("**① Plan-and-Execute · 计划**（动手前先拆解目标）")
        for i, s in enumerate(run.plan, 1):
            L.append(f"{i}. 用 `{s.get('tool', '?')}` — {s.get('why', '')}")
        L.append("")

    # ② ReAct：Thought → Action → Observation 循环，真正调用工具。
    L.append("**② ReAct · 执行**（想 Thought → 做 Action → 看 Observation）")
    if not run.transcript:
        L.append("- （无）")
    for item in run.transcript:
        t = item.get("type")
        if t == "thought":
            L.append(f"- 💭 **想**：{_clip(item.get('content', ''))}")
        elif t == "action":
            L.append(f"- 🔧 **做**：`{item.get('tool')}[{_clip(item.get('input', ''), 60)}]`")
            L.append(f"    ↳ 👀 **看**：{_summarize_obs(item.get('observation', {}))}")
        elif t == "finish":
            L.append(f"- 🏁 **收**：{_clip(item.get('content', ''), 200)}")
    L.append("")

    # ③ Reflection：行动后自评是否达标，不达标则重规划。
    if run.reflections:
        L.append("**③ Reflection · 反思**（自评达标了吗，不达标就重规划）")
        for i, v in enumerate(run.reflections, 1):
            done = "✅ 达标" if v.get("complete") else "↻ 继续"
            extra = ""
            if v.get("missing"):
                extra += f"，缺口：{'; '.join(map(str, v.get('missing', [])))}"
            if v.get("next"):
                extra += f"，下一步：{_clip(v.get('next'), 60)}"
            L.append(f"- 第 {i} 轮：{done}，打分 {v.get('score', '?')}/10{extra}")

    if run.failures:
        L.append("")
        L.append(f"**⚠️ 失败记录**：{len(run.failures)} 条（容错：记录但不阻断整体）")

    return "\n".join(L)


def _judge_repo(repo: str, cap: int = 5):
    """对一个仓库的盲区逐个跑 judge_intent（含 RAG + 上下文构建 + 压缩），返回 (主建议, 轨迹④)。"""
    main: List[str] = []
    trace: List[str] = []
    try:
        units = scan_repo(repo).units              # 完整 CodeUnit（judge 需要 calls 等）
    except Exception:  # noqa: BLE001
        return "", ""
    by_id = {u.unit_id: u for u in units}
    # 只对「当前算盲区」的函数判定（尊重反馈抑制）。
    spots = [u for u in units if signals_of(u) and not u.has_instrumentation
             and not _get_memory().is_ignored(repo, u.unit_id)]
    if not spots:
        return "", ""
    index = CodeIndex(embedder=_get_embedder())    # 建索引供 RAG 召回相似已埋点函数
    index.index(units)
    for unit in spots[:cap]:
        v = judge_intent(unit, index, _LLM, repo=repo,
                         notes=_get_notes(), episodic=_get_memory())
        sigs = ", ".join(signals_of(unit)) or "-"

        verb = "建议 **埋点**" if v.verdict == "instrument" else "可跳过"
        flag = {"uncertain": " ⚠️存疑", "llm_unavailable": " （无 LLM·通用建议）",
                "parse_error": " ⚠️解析失败"}.get(v.status, "")
        main.append(f"- **`{unit.qualname}`** [{sigs}] → {verb}（置信 {v.confidence:.1f}{flag}）")
        for s in v.suggestions[:4]:
            main.append(f"    - {s.get('type', '?')}: {s.get('what', '')}")
        if v.evidence:
            main.append(f"    - 依据：{', '.join(map(str, v.evidence[:4]))}")

        # 轨迹④：展示 ContextBuilder 实际拼了哪些上下文 + 压缩情况。
        kept = [c for c in v.context if c.get("included")]
        used = sum(c.get("tokens", 0) for c in kept)
        by_src: dict = {}
        for c in kept:
            by_src[c["source"]] = by_src.get(c["source"], 0) + 1
        comp = ", ".join(f"{k}×{n}" for k, n in by_src.items()) or "无"
        shrunk = [c for c in kept if c.get("level") in ("compact", "clipped", "summarized")]
        dropped = [c for c in v.context if not c.get("included")]
        notes = []
        if shrunk:
            notes.append(f"压缩 {len(shrunk)} 段（{', '.join(sorted({c['level'] for c in shrunk}))}）")
        if dropped:
            notes.append(f"预算外丢弃 {len(dropped)} 段")
        tail = ("；" + "；".join(notes)) if notes else ""
        trace.append(
            f"- `{unit.qualname}`：拼上下文 [{comp}] 共约 {used} tokens{tail}"
            f" → 判定 **{v.verdict}** / {v.confidence:.1f}"
        )

    main_md = ("### 🧠 意图判定（该埋什么）\n" + "\n".join(main)) if main else ""
    trace_md = ("**④ 上下文构建 + 压缩 + Judge**（多源证据 → 去重/降级/预算 → LLM 判定）\n"
                + "\n".join(trace)) if trace else ""
    return main_md, trace_md


def _judge_section(run: AgentRun):
    """对一次 run 里扫到的仓库跑判定（取第一个有盲区的仓库），返回 (主建议, 轨迹④)。"""
    for rep in _scan_reports(run):
        repo = rep.get("repo")
        if repo and rep.get("blind_spots"):
            return _judge_repo(repo)
    return "", ""



def _format_run(run: AgentRun) -> str:
    """把一次 agent 自治运行渲染成对话回复：结论 + 判定建议 + 可展开的过程。"""
    parts: List[str] = []

    # 1) 事实层：盲区表（ground truth）。
    reports = _scan_reports(run)
    if reports:
        parts.append("\n\n".join(_render_report(r) for r in reports))

    # 2) 判定层：对盲区逐个判定「该埋什么」（RAG + 上下文 + LLM）。
    judge_main, judge_trace = _judge_section(run)
    if judge_main:
        parts.append(judge_main)

    # 3) 解读层：LLM 的自然语言结论（仅散文，避免与表格重复）。
    ans = (run.answer or "").strip()
    if ans and not ans.startswith("{"):
        parts.append("💬 " + ans)
    elif not reports and not judge_main:
        parts.append(ans or "（没有得到结论）")

    # 4) 学习层：三范式执行轨迹 + RAG/判定证据，可展开。
    trace = _format_trace(run)
    if judge_trace:
        trace = trace + "\n\n" + judge_trace
    if trace.strip():
        parts.append(
            f"\n<details><summary>🔎 Agent 执行轨迹 · 三范式 + RAG/判定（{run.rounds} 轮，点击展开）</summary>"
            f"\n\n{trace}\n</details>"
        )
    return "\n".join(parts)


def _report_with_judge(report: dict, repo: str) -> str:
    """扫描报告 → 表格 + 意图判定 + 可展开的「上下文构建/压缩」轨迹（有 LLM 才判定）。"""
    out = _render_report(report)
    if _LLM.available:
        main, trace = _judge_repo(repo)
        if main:
            out += "\n\n" + main
        if trace:
            out += ("\n\n<details><summary>🔎 上下文构建 + 压缩 + 判定（点击展开）</summary>"
                    f"\n\n{trace}\n</details>")
    return out


def _format_scan_result(path: str) -> str:
    """降级模式：不经 LLM，直接扫描并渲染盲区表（文件名可点击跳 VS Code）。

    仍走带记忆的 scan 工具，这样反馈抑制/运行记录在无 LLM 时同样生效。
    """
    report = build_scan_tool(broker=None, memory=_get_memory(), notes=_get_notes()).func(path)
    if isinstance(report, dict) and "blind_spots" in report:
        return _report_with_judge(report, path)
    return f"扫描 `{path}` 未返回结果。"


def _extract_path(message: str) -> Optional[str]:
    """从消息里提取一个真实存在的路径（降级模式用；确定性，不靠 LLM）。"""
    # 先看引号里的内容，再看含 / 或 ~ 的 token。
    candidates: List[str] = re.findall(r'["\']([^"\']+)["\']', message)
    candidates += [tok for tok in message.split() if ("/" in tok or tok.startswith("~"))]
    for c in candidates:
        p = os.path.expanduser(c.strip().rstrip(".,;:"))
        if os.path.exists(p):
            return p
    return None


# -- 授权回合：识别同意/拒绝、收集待授权请求 --------------------------------

# 肯定/否定用词（中英）。授权是显式动作，不做模糊猜测，只认明确表态。
_AFFIRM = ("同意", "授权", "允许", "批准", "准许", "可以", "好的", "好", "行")
_AFFIRM_EN = {"ok", "okay", "yes", "y", "sure", "approve", "allow", "grant"}
_DENY = ("不同意", "拒绝", "取消", "不行", "不要", "别", "否")
_DENY_EN = {"no", "n", "cancel", "deny", "stop"}


def _norm(msg: str) -> str:
    return msg.strip().strip("。.!！,，").lower()


def _is_affirmative(msg: str) -> bool:
    m = _norm(msg)
    return m in _AFFIRM_EN or any(k in msg for k in _AFFIRM)


def _is_negative(msg: str) -> bool:
    m = _norm(msg)
    return m in _DENY_EN or any(k in msg for k in _DENY)


def _collect(run: AgentRun, key: str) -> List[str]:
    """从 transcript 的 scan 观测里收集带某个 key 的路径（permission_required / denied）。"""
    out: List[str] = []
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") == "scan":
            res = item.get("observation", {}).get("result", {})
            if isinstance(res, dict) and res.get(key) and res[key] not in out:
                out.append(res[key])
    return out


def _all_matches(run: AgentRun) -> List[str]:
    """收集 find_repo 找到的全部候选路径（用于「多候选让用户选」）。"""
    out: List[str] = []
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") == "find_repo":
            res = item.get("observation", {}).get("result", {})
            for m in (res.get("matches") or []):
                if m not in out:
                    out.append(m)
    return out


def _format_permission_request(paths: List[str], broker: PermissionBroker,
                               candidates: Optional[List[str]] = None) -> str:
    candidates = candidates or []
    lines = ["🔐 **需要你的授权**", ""]
    if candidates:
        lines.append("我找到**多个**匹配的项目——请在下方**选择**要扫的那个，再点 **✅ 同意扫描**：")
        lines += [f"- `{c}`" for c in candidates]
    else:
        lines.append("我定位到下面的目录，读取它的**代码内容**需要你点头（点 **✅ 同意扫描**）：")
        lines += [f"- `{p}`" for p in paths]
    lines += ["", f"_允许范围：`{broker.root}`_"]
    return "\n".join(lines)


def _format_denied(paths: List[str], broker: PermissionBroker) -> str:
    lines = ["⛔ **超出允许范围，已拒绝**", ""]
    lines += [f"- `{p}`" for p in paths]
    lines += ["", f"_只能访问：`{broker.root}`（可用环境变量 SENTINEL_WORKSPACE_ROOT 调整）_"]
    return "\n".join(lines)


# -- 对话回调 --------------------------------------------------------------

def _run_and_format(broker: PermissionBroker, goal: str, state: dict) -> str:
    """跑一次 agent；若撞到权限门则转成授权请求，否则渲染结果。"""
    state["goal"] = goal
    run = _build_agent(broker).run(goal=goal)
    pending = _collect(run, "permission_required")
    if pending:
        state["pending"] = pending
        return _format_permission_request(pending, broker)
    denied = _collect(run, "denied")
    if denied:
        return _format_denied(denied, broker)
    return _format_run(run)


def respond(message: str, history, state) -> str:
    """ChatInterface 回调：一条消息 = 一个目标 / 或对上一条授权请求的回应。"""
    msg = (message or "").strip()
    if not isinstance(state, dict):
        state = {}
    if "broker" not in state:  # 懒初始化会话态（每个浏览器会话独立）
        state["broker"] = PermissionBroker(workspace_root())
        state["pending"] = []
        state["goal"] = None
    broker: PermissionBroker = state["broker"]

    if not msg:
        return "跟我说个目标吧，比如：**扫一下 sentinel-sample-app**"

    # 若上一回合留了待授权请求，先把这条消息当作「同意/拒绝」处理。
    if state.get("pending"):
        if _is_affirmative(msg):
            granted = []
            for p in state["pending"]:
                try:
                    granted.append(broker.grant(p))
                except PermissionError:
                    pass
            goal = state.get("goal")
            state["pending"] = []
            if _LLM.available and goal:
                return _run_and_format(broker, goal, state)
            # 降级：无 LLM，直接扫已授权路径。
            return "\n\n".join(_format_scan_result(p) for p in granted) or "已授权。"
        if _is_negative(msg):
            state["pending"] = []
            return "好的，已取消，不读取该目录。"
        state["pending"] = []  # 既非同意也非拒绝 → 当作新目标继续

    if not _LLM.available:
        # 降级：无 LLM。能认出 scope 内路径就直接扫（对话里显式发起=隐含同意）。
        path = _extract_path(msg)
        note = f"_（当前无 LLM key，纯扫描模式：{_LLM.why_unavailable()}）_\n\n"
        if path and broker.within_scope(path):
            return note + _format_scan_result(path)
        if path:
            return note + f"⚠️ `{path}` 超出允许范围（{broker.root}）。"
        return note + f"给我一个（{broker.root} 内的）仓库路径或项目名。"

    return _run_and_format(broker, msg, state)


# -- Blocks UI 处理器（按钮 / 单选 / 可点击结果）-------------------------------

def _ensure(state) -> dict:
    """懒初始化会话态（每个浏览器会话独立）。"""
    if not isinstance(state, dict):
        state = {}
    if "broker" not in state:
        state["broker"] = PermissionBroker(workspace_root())
        state["pending"] = []
        state["candidates"] = []
        state["goal"] = None
    return state


def _controls(pending=None, candidates=None):
    """返回三个控件的更新：候选单选、同意按钮、取消按钮。"""
    pending = pending or []
    candidates = candidates or []
    show = bool(pending)
    radio = gr.update(visible=bool(candidates), choices=candidates,
                      value=(candidates[0] if candidates else None))
    return radio, gr.update(visible=show), gr.update(visible=show)


def _ignore_controls(state):
    """返回「忽略」下拉与按钮的更新：有上次扫描盲区时才显示，选项=各盲区 unit_id。"""
    spots = (state.get("last_scan") or {}).get("spots", [])
    choices = [s["unit_id"] for s in spots]
    vis = bool(choices)
    return gr.update(visible=vis, choices=choices, value=[]), gr.update(visible=vis)


def _stash_scan(state: dict, report) -> None:
    """把一次扫描的盲区快照存进会话态，供下一轮解析「把这个忽略」这类指代。"""
    if not isinstance(report, dict) or "blind_spots" not in report:
        return
    state["last_scan"] = {
        "repo": report.get("repo", ""),
        "spots": [
            {"unit_id": b.get("unit_id") or f"{b.get('file')}::{b.get('function')}",
             "function": b.get("function", ""),
             "signals": b.get("signals", [])}
            for b in report.get("blind_spots", [])
        ],
    }


def _stash_scan_from_run(state: dict, run: AgentRun) -> None:
    """从一次 agent run 的 transcript 里取最后一份扫描报告，存快照。"""
    for rep in reversed(_scan_reports(run)):
        if isinstance(rep, dict) and rep.get("blind_spots"):
            _stash_scan(state, rep)
            return


def _recent_context(chat, state: dict, max_turns: int = 4) -> str:
    """拼「会话背景」——走**同一套 ContextBuilder**（预算/去重/压缩/溯源一致）。

    与 judge 的区别只是换了 provider：LastScan（上次盲区）+ Note（仓库约定）+ Conversation。
    这样「每一次 LLM 调用（plan/act/judge）都遵守同一套上下文纪律」。
    """
    from sentinel.cognition.context_builder import ContextTarget, default_turn_builder
    last = state.get("last_scan")
    turns = [(m["role"], m.get("content", ""))
             for m in (chat or []) if m.get("role") in ("user", "assistant")][:-1]
    repo = (last or {}).get("repo", "")
    target = ContextTarget(repo=repo, turns=turns[-max_turns:], last_scan=last)
    ctx = default_turn_builder(notes=_get_notes(), episodic=_get_memory()).build(target)
    return ctx.text


def _agent_turn(state: dict, goal: str, context: str = ""):
    """跑一次 agent，返回 (回复文本, 待授权路径, 候选路径)。

    context：会话背景（近期对话 + 上次扫描盲区），让「把这个忽略」这类指代能解析。
    """
    broker: PermissionBroker = state["broker"]
    state["goal"] = goal

    if not _LLM.available:  # 降级：无 LLM，能认出 scope 内路径就直接扫
        path = _extract_path(goal)
        note = f"_（无 LLM key，纯扫描模式：{_LLM.why_unavailable()}）_\n\n"
        if path and broker.within_scope(path):
            _authorize_open(path)  # 显式发起扫描 = 隐含同意，其文件可被 /open 打开
            report = build_scan_tool(broker=None, memory=_get_memory(), notes=_get_notes()).func(path)
            _stash_scan(state, report)
            return note + (_render_report(report) if isinstance(report, dict)
                           and "blind_spots" in report else str(report)), [], []
        if path:
            return note + f"⚠️ `{path}` 超出允许范围（{broker.root}）。", [], []
        return note + f"给我一个（{broker.root} 内的）仓库路径或项目名。", [], []

    run = _build_agent(broker).run(goal=goal, context=context)
    _stash_scan_from_run(state, run)                       # 记下本轮扫描盲区，供下一轮指代
    pending = _collect(run, "permission_required")
    if pending:
        matches = _all_matches(run)
        candidates = matches if len(matches) > 1 else []
        return _format_permission_request(pending, broker, candidates), pending, candidates
    denied = _collect(run, "denied")
    if denied:
        return _format_denied(denied, broker), [], []
    return _format_run(run), [], []


def _approve(state: dict, selected: Optional[str]):
    """授权并直接扫描被选中的目标（确定性，尊重用户的候选选择）。"""
    broker: PermissionBroker = state["broker"]
    target = selected or (state["pending"][0] if state.get("pending") else None)
    state["pending"] = []
    state["candidates"] = []
    if not target:
        return "没有待授权的目标。"
    try:
        broker.grant(target)
    except PermissionError as e:
        return f"⛔ {e}"
    _authorize_open(target)  # 授权成功 → 其下文件今后可被 /open 打开
    report = build_scan_tool(broker, memory=_get_memory(), notes=_get_notes()).func(target)
    if isinstance(report, dict) and "blind_spots" in report:
        _stash_scan(state, report)                        # 存盲区快照，供下一轮指代 + 忽略按钮
        return _report_with_judge(report, target)
    return _format_scan_result(target)  # 兜底


def _ui_add_user(message: str, chat):
    """第一步（秒回）：立刻把用户消息显示到聊天区并清空输入框。"""
    chat = list(chat or [])
    msg = (message or "").strip()
    if msg:
        chat.append({"role": "user", "content": msg})
    return chat, ""


def _ui_bot(chat, state):
    """第二步（可能耗时）：读最后一条用户消息，跑 agent / 处理授权，补回复。"""
    state = _ensure(state)
    chat = list(chat or [])
    if not chat or chat[-1].get("role") != "user":
        return chat, *_controls(state.get("pending"), state.get("candidates")), \
            *_ignore_controls(state)
    msg = chat[-1]["content"]

    # 有待授权时，也支持打字「同意/取消」（与按钮等效）。
    if state.get("pending") and _is_affirmative(msg):
        reply, pending, candidates = _approve(state, None), [], []
    elif state.get("pending") and _is_negative(msg):
        state["pending"] = []
        state["candidates"] = []
        reply, pending, candidates = "好的，已取消，不读取该目录。", [], []
    else:
        state["pending"] = []
        state["candidates"] = []
        ctx = _recent_context(chat, state)               # 会话背景（上次扫描 + 近期对话）
        reply, pending, candidates = _agent_turn(state, msg, context=ctx)
        state["pending"] = pending
        state["candidates"] = candidates

    chat.append({"role": "assistant", "content": reply})
    return chat, *_controls(pending, candidates), *_ignore_controls(state)


def _ui_approve(chat, state, selected):
    state = _ensure(state)
    chat = list(chat or [])
    reply = _approve(state, selected)
    chat.append({"role": "user", "content": f"✅ 同意扫描 `{selected or (state.get('goal') or '')}`"})
    chat.append({"role": "assistant", "content": reply})
    return chat, *_controls([], []), *_ignore_controls(state)


def _ui_deny(chat, state):
    state = _ensure(state)
    chat = list(chat or [])
    state["pending"] = []
    state["candidates"] = []
    chat.append({"role": "user", "content": "❌ 取消"})
    chat.append({"role": "assistant", "content": "好的，已取消，不读取该目录。"})
    return chat, *_controls([], []), *_ignore_controls(state)


def _ui_ignore(chat, state, selected):
    """把选中的盲区函数标记为「不用埋点」（写入反馈记忆），下次扫描自动抑制。"""
    from sentinel.memory import IGNORE
    state = _ensure(state)
    chat = list(chat or [])
    last = state.get("last_scan") or {}
    repo = last.get("repo")
    picks = list(selected or [])
    if repo and picks:
        mem = _get_memory()
        for uid in picks:
            mem.record_feedback(repo, uid, IGNORE)
        last["spots"] = [s for s in last.get("spots", []) if s["unit_id"] not in set(picks)]
        state["last_scan"] = last
        names = "、".join(f"`{u}`" for u in picks)
        chat.append({"role": "user", "content": f"🚫 忽略 {len(picks)} 个函数"})
        chat.append({"role": "assistant",
                     "content": f"已把 {names} 标记为「不用埋点」，下次扫描将自动抑制（反馈学习）。"})
    return chat, *_controls([], []), *_ignore_controls(state)


def build_demo() -> "gr.Blocks":
    """自定义 Blocks 界面：对话 + 授权按钮 + 多候选单选 + 结果可点击跳 VS Code。"""
    root = workspace_root()
    status = "✅ 已连接 LLM" if _LLM.available else "⚠️ 纯扫描模式（未配 LLM key）"

    with gr.Blocks(title="Sentinel · 可观测性守护 Agent") as demo:
        gr.Markdown(
            f"# 🛡️ Sentinel · 可观测性守护 Agent\n"
            f"读懂代码、找出监控盲区（调了依赖却没埋点的函数）。当前状态：{status}。\n"
            f"允许范围：`{root}`。只给项目名也行，我会先找路径、再请你**授权**后扫描；"
            "结果里的文件名可**点击在 VS Code 打开**。"
        )
        state = gr.State({})
        chatbot = gr.Chatbot(type="messages", height=440, label="Sentinel", show_copy_button=True)
        cand_radio = gr.Radio(choices=[], visible=False, label="找到多个匹配，选一个授权：")
        with gr.Row():
            approve_btn = gr.Button("✅ 同意扫描", variant="primary", visible=False)
            deny_btn = gr.Button("❌ 取消", visible=False)
        with gr.Row():
            ignore_dd = gr.Dropdown(choices=[], value=[], multiselect=True, visible=False,
                                    scale=8, label="🚫 选不用埋点的函数（忽略后下次扫描不再报）")
            ignore_btn = gr.Button("🚫 忽略选中", visible=False, scale=1)
        with gr.Row():
            msg = gr.Textbox(placeholder="扫一下 sentinel-sample-app", show_label=False, scale=8, autofocus=True)
            send = gr.Button("发送", variant="primary", scale=1)

        outs_btn = [chatbot, cand_radio, approve_btn, deny_btn, ignore_dd, ignore_btn]
        # 两步：先秒回显示用户消息+清空输入框，再 .then() 跑 agent 补回复。
        add_io = ([msg, chatbot], [chatbot, msg])
        send.click(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)
        msg.submit(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)

        approve_btn.click(_ui_approve, [chatbot, state, cand_radio], outs_btn, api_name=False)
        deny_btn.click(_ui_deny, [chatbot, state], outs_btn, api_name=False)
        ignore_btn.click(_ui_ignore, [chatbot, state, ignore_dd], outs_btn, api_name=False)

    return demo


def run(host: str = "127.0.0.1", port: int = 7860) -> None:
    """启动：把 Gradio 挂在自建 FastAPI 上，额外提供 /open 端点（点文件名打开 VS Code）。"""
    import uvicorn
    from fastapi import FastAPI, Response

    app = FastAPI()

    @app.get("/open")
    def _open(path: str, line: int = 1):  # noqa: ANN202
        _open_in_editor(path, line)
        # 204 No Content：点链接后浏览器不跳转、对话不丢，只默默打开编辑器。
        return Response(status_code=204)

    @app.on_event("startup")
    def _warm_up() -> None:
        """后台预热 embedder（首次会下载 ONNX 模型，同步做的话会卡住用户第一次点击）。

        放进后台线程、不阻塞服务启动；判定时若还没预热完，_get_embedder() 会自己再等。
        """
        import threading

        def _warm():
            try:
                _get_embedder().embed_one("warm up")
            except Exception:  # noqa: BLE001  预热失败不影响服务；真正用到时会再报错
                pass

        threading.Thread(target=_warm, daemon=True).start()

    app = gr.mount_gradio_app(app, build_demo(), path="/")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
