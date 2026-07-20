"""Sentinel 对话式 Web 界面（Gradio ChatInterface）。

用户在对话里下达目标（如「扫一下 /path/to/repo」），交给三范式 AgentCore 自治执行：
    plan → act(ReAct，真实调用 scan 工具) → reflect
配了 LLM key 时是真正的 agent 推理；没配 key 时降级为「纯扫描直报」，仍可用。

运行：PYTHONPATH=src python3 -m sentinel.webapp
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
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

from sentinel.config import LocalIdentity, workspace_root
from sentinel.engines.agent import AgentCore, AgentRun
from sentinel.engines.agent_tools import (
    build_find_repo_tool,
    build_scan_tool,
    build_scan_changed_tool,
    build_check_language_tool,
    build_install_language_tool,
    build_register_dynamic_language_tool,
    build_feedback_tool,
    build_note_tool,
    build_recall_notes_tool,
    build_apply_tool,
    build_telemetry_plan_tool,
    build_dashboard_tool,
    build_deploy_dashboard_tool,
)
from sentinel.engines.scan import scan_repo, signals_of
from sentinel.engines.judge import judge_intent
from sentinel.cognition import CodeIndex
from sentinel.llm import LLMClient
from sentinel.permissions import PermissionBroker
import logging

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

# 程序性记忆单例（修复技能：补埋点时复用/记录）。
_PROCEDURAL = None


def _get_procedural():
    global _PROCEDURAL
    if _PROCEDURAL is None:
        from sentinel.memory import ProceduralMemory
        _PROCEDURAL = ProceduralMemory()
    return _PROCEDURAL


# 外部 MCP 工具只在首次构造 Agent 时发现一次；配置变更后重启进程即可刷新。
# 连接失败会缓存为空，避免每轮对话都等待远程超时，Sentinel 本地能力照常可用。
_MCP_TOOLS = None


def _get_mcp_tools():
    global _MCP_TOOLS
    if _MCP_TOOLS is None:
        from sentinel.integrations import MCPClientManager, configured_mcp_servers
        servers = configured_mcp_servers()
        _MCP_TOOLS = MCPClientManager(servers).discover() if servers else {}
    return dict(_MCP_TOOLS)


# 本地协作原型：身份/工作区写入本地 SQLite；后续可替换为云端实现。
_COLLABORATION = None


def _get_collaboration(identity: Optional[LocalIdentity] = None):
    global _COLLABORATION
    identity = identity or LocalIdentity.from_env()
    if _COLLABORATION is None:
        from sentinel.memory import CollaborationStore
        _COLLABORATION = CollaborationStore()
    _COLLABORATION.ensure_user(identity.user_id, identity.display_name)
    _COLLABORATION.ensure_workspace(identity.workspace_id, identity.workspace_name,
                                    identity.user_id)
    return _COLLABORATION

# 意图判定用的向量索引 embedder 单例（避免每条消息重载模型）。
_EMBEDDER = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentinel.cognition import default_embedder
        _EMBEDDER = default_embedder()
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
    scan_changed = build_scan_changed_tool(broker, memory=mem)  # git 增量：只扫本分支改动到的函数
    check = build_check_language_tool(broker)             # 只读：报告语言覆盖缺口
    install = build_install_language_tool(_LLM)           # 破坏性：须用户明确同意后才补齐
    register_language = build_register_dynamic_language_tool(_LLM)
    ignore = build_feedback_tool(mem)                     # 反馈学习：标为不用埋点→下次抑制
    add_note = build_note_tool(notes, mem)               # 记团队笔记 → 判定上下文一等证据
    recall = build_recall_notes_tool(notes, mem)         # 查团队笔记
    apply = build_apply_tool(broker, memory=mem, notes=notes, procedural=_get_procedural(), llm=_LLM)  # 结构化执行器：LLM 填 targets，直接改文件未提交
    telemetry_plan = build_telemetry_plan_tool(broker)
    dashboard = build_dashboard_tool(broker)
    deploy_dashboard = build_deploy_dashboard_tool(broker)
    tools = {t.name: t for t in (find, scan, scan_changed, check, install, register_language, ignore, add_note, recall, apply, telemetry_plan, dashboard, deploy_dashboard)}
    tools.update(_get_mcp_tools())
    return AgentCore(_LLM, tools=tools)


# -- 回复格式化 ------------------------------------------------------------

def _scan_reports(run: AgentRun) -> List[dict]:
    """从 transcript 里取出 scan 工具的真实观测（ground truth，不用 LLM 改写过的答案）。"""
    reports: List[dict] = []
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") in ("scan", "scan_changed"):
            obs = item.get("observation", {})
            if isinstance(obs.get("result"), dict):
                reports.append(obs["result"])
    return reports


def _open_link(repo: str, rel_file: str, lines: str) -> str:
    """拼一个在新标签页打开的本机 /open 链接。

    为什么不用 vscode:// ：Gradio 前端会把非 http 的自定义协议链接过滤掉（点不动）。
    改成同源 http 链接就不会被清洗；点击时服务端用 code -g 打开（见 /open 路由）。
    必须新开页面，避免 /open 成功页替换正在进行的 Sentinel 对话。
    """
    abspath = rel_file if os.path.isabs(rel_file) else os.path.join(repo or "", rel_file)
    start = str(lines or "1").split("-")[0].strip() or "1"
    return f"/open?path={quote(abspath)}&line={start}"


def _vscode_uri(path: str, line: int) -> str:
    """构造跨平台的 VS Code 文件 URI，保留 Windows 盘符和路径分隔符。"""
    normalized = os.path.abspath(path).replace("\\", "/")
    return f"vscode://file/{quote(normalized, safe='/:')}:{max(1, int(line))}"


def _code_executable() -> Optional[str]:
    """定位 VS Code CLI 或 Windows 标准安装目录中的 Code.exe。"""
    command = shutil.which("code")
    if command:
        return command
    if os.name != "nt":
        return None
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft VS Code", "Code.exe"),
    ]
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)


def _open_in_editor(path: str, line: int) -> tuple[bool, str]:
    """服务端在本机用 VS Code 打开文件到指定行。

    双重门（§14）：既要在工作区范围内，又要该目录**已被授权**（与 scan 一致）——
    没授权过的仓库文件一律不开，即使有人构造链接也没用。
    """
    root = workspace_root()
    ap = os.path.abspath(path)
    in_scope = ap == root or ap.startswith(root + os.sep)
    if not in_scope:
        return False, "文件超出允许范围。"
    if not _is_open_authorized(ap):
        return False, "该目录尚未获得扫描授权。"
    if not os.path.exists(ap):
        return False, "目标文件不存在。"
    code = _code_executable()
    try:
        if code:  # CLI 或 Code.exe 均支持 --goto，能直接聚焦编辑器与目标行。
            result = subprocess.run([code, "--goto", f"{ap}:{line}"], check=False)
            if result.returncode == 0:
                return True, "已请求 VS Code 打开文件。"
            return False, f"VS Code 命令退出码为 {result.returncode}。"

        uri = _vscode_uri(ap, line)
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]  # Windows 协议处理器
        elif sys.platform == "darwin":
            subprocess.run(["open", uri], check=True)
        else:
            subprocess.run(["xdg-open", uri], check=True)
        return True, "已请求 VS Code 打开文件。"
    except Exception as exc:  # noqa: BLE001
        return False, f"无法启动 VS Code：{exc}"


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
        file_name = html.escape(str(b.get("file", "?")))
        file_cell = f'<a href="{link}" target="_blank" rel="noopener">{file_name}</a>'
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
        call_id = obs.get("call_id")
        suffix = f"（call_id: `{call_id}`）" if call_id else ""
        return f"⚠️ {_clip(obs['error'])}{suffix}"
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


def _format_injected_context(run: AgentRun) -> str:
    """显示本轮压缩后实际传给 Agent 的上下文，便于诊断召回与指代问题。"""
    context = (run.context or "").strip()
    if not context:
        context = "（本轮没有额外上下文。）"
    # 防止上下文里的 Markdown 围栏提前结束当前代码块。
    safe_context = context.replace("```", "``\u200b`")
    return (
        "<details><summary>🧩 实际注入 LLM 的上下文（压缩后，点击展开）</summary>\n\n"
        "以下内容会作为 `CONTEXT` 拼入本轮 Agent 的系统提示：\n\n"
        f"```text\n{safe_context}\n```\n"
        "</details>"
    )


def _format_rewrite_trace(run: AgentRun) -> str:
    """显示 LLM query rewrite 的事实输入、草案、原始输出和校验结果。"""
    trace = run.rewrite_trace or {}
    if not trace:
        return ""
    safe = json.dumps(trace, ensure_ascii=False, indent=2).replace("```", "``\u200b`")
    return (
        "<details><summary>🧭 Query rewrite 与约束校验（点击展开）</summary>\n\n"
        "以下记录包含原始输入、允许使用的事实、确定性草案、LLM 原始 JSON，以及验证后的结果。\n\n"
        f"```json\n{safe}\n```\n"
        "</details>"
    )


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

    # 4) 诊断层：展示压缩后真实注入 LLM 的上下文，避免上下文丢失不可见。
    rewrite = _format_rewrite_trace(run)
    if rewrite:
        parts.append(rewrite)
    parts.append(_format_injected_context(run))

    # 5) 学习层：三范式执行轨迹 + RAG/判定证据，可展开。
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
        if item.get("type") == "action" and item.get("tool") in ("scan", "scan_changed"):
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
    _stash_focus_from_run(state, run)
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
        state["focus_repo"] = ""
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
        state["focus_repo"] = ""
    if "identity" not in state:
        identity = LocalIdentity.from_env()
        _get_collaboration(identity)
        state["identity"] = identity
    state.setdefault("pending_apply", None)
    state.setdefault("focus_repo", "")
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
        "language_gap": report.get("language_gap") or {},
        "spots": [
            {"unit_id": b.get("unit_id") or f"{b.get('file')}::{b.get('function')}",
             "function": b.get("function", ""),
             "signals": b.get("signals", [])}
            for b in report.get("blind_spots", [])
        ],
    }
    state["focus_repo"] = report.get("repo", "")


def _stash_scan_from_run(state: dict, run: AgentRun) -> None:
    """从一次 agent run 的 transcript 里取最后一份扫描报告，存快照。"""
    for rep in reversed(_scan_reports(run)):
        if isinstance(rep, dict) and "blind_spots" in rep:
            _stash_scan(state, rep)
            return


def _stash_focus_from_run(state: dict, run: AgentRun) -> None:
    """把本轮最近确认的本地仓库写成对话焦点，不冒充扫描快照。"""
    for item in reversed(run.transcript):
        if item.get("type") != "action":
            continue
        result = (item.get("observation") or {}).get("result")
        if not isinstance(result, dict):
            continue
        local_repo = result.get("local_repo")
        if isinstance(local_repo, str) and os.path.isdir(local_repo):
            state["focus_repo"] = os.path.abspath(local_repo)
            return
        if item.get("tool") == "find_repo":
            matches = result.get("matches") or []
            if len(matches) == 1 and os.path.isdir(matches[0]):
                state["focus_repo"] = os.path.abspath(matches[0])
                return


def _focus_snapshot(state: dict) -> dict:
    """给 ContextBuilder/query rewrite 的一致事实；焦点切换后不混入旧扫描。"""
    last = state.get("last_scan") or {}
    focus = str(state.get("focus_repo") or "").strip()
    last_repo = str(last.get("repo") or "").strip()
    if focus and os.path.normcase(os.path.abspath(focus)) != os.path.normcase(os.path.abspath(last_repo)):
        return {"repo": focus, "language_gap": {}, "spots": []}
    if last:
        return last
    return {"repo": focus, "language_gap": {}, "spots": []} if focus else {}


def _recent_context(chat, state: dict, max_turns: int = 4) -> str:
    """拼「会话背景」——走**同一套 ContextBuilder**（预算/去重/压缩/溯源一致）。

    与 judge 的区别只是换了 provider：LastScan（上次盲区）+ Note（仓库约定）+ Conversation。
    这样「每一次 LLM 调用（plan/act/judge）都遵守同一套上下文纪律」。
    """
    from sentinel.cognition.context_builder import ContextTarget, default_turn_builder
    last = _focus_snapshot(state)
    turns = [(m["role"], _message_text(m.get("content")))
             for m in (chat or []) if m.get("role") in ("user", "assistant")][:-1]
    repo = (last or {}).get("repo", "")
    target = ContextTarget(repo=repo, focus_repo=state.get("focus_repo", ""),
                           turns=turns[-max_turns:], last_scan=last)
    ctx = default_turn_builder(notes=_get_notes(), episodic=_get_memory()).build(target)
    return ctx.text


def _collect_apply_result(run):
    """从 transcript 取 apply_instrumentation 的执行结果（含 diff）。"""
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") == "apply_instrumentation":
            res = (item.get("observation") or {}).get("result", {})
            if isinstance(res, dict) and res.get("applied"):
                return res["applied"]
    return None


def _format_applied(a: dict) -> str:
    lines = [f"✅ {a.get('message', '已补埋点')}"]
    if a.get("units_fixed"):
        lines.append(f"已补 {len(a['units_fixed'])} 个：{', '.join(a['units_fixed'])}")
    if a.get("skipped"):
        lines.append(f"⏭ 跳过 {len(a['skipped'])} 个（非 Python / 改写不安全）")
    if a.get("emitter"):
        configured = a.get("receiver_configured")
        receiver = "已检测到配置" if configured is True else (
            "未检测到配置" if configured is False else "未验证")
        lines.append(
            f"遥测状态：emitter=`{a['emitter']}`；Receiver={receiver}；"
            f"delivery=`{a.get('delivery', 'unverified')}`")
    if a.get("delivery_note"):
        lines.append(a["delivery_note"])
    diff = a.get("diff", "")
    if diff:
        lines.append(f"\n```diff\n{diff}\n```")
    return "\n".join(lines)


def _collect_apply_proposal(run):
    """从 transcript 取 apply_instrumentation 工具的补埋点提议（若有）。"""
    for item in run.transcript:
        if item.get("type") == "action" and item.get("tool") == "apply_instrumentation":
            res = (item.get("observation") or {}).get("result", {})
            if isinstance(res, dict) and res.get("proposed_apply"):
                return res["proposed_apply"]
    return None


def _do_apply(repo: str) -> str:
    """真正执行补埋点：扫盲区→学约定→Applier 直接改文件（未提交）→回 diff。"""
    from sentinel.engines.apply import Applier, ApplyError
    from sentinel.engines.conventions import learn_convention
    mem = _get_memory()
    result = scan_repo(repo)
    ignored = mem.ignored_units(repo)
    spots = [u for u in result.blind_spots if u.unit_id not in ignored]
    if not spots:
        return "没有需要补埋点的盲区了。"
    conv = learn_convention(repo, result.units)
    try:
        res = Applier(llm=_LLM).apply(repo, spots, convention=conv, procedural=_get_procedural())
    except ApplyError as e:
        return f"❌ 无法补埋点：{e}"
    lines = [f"✅ {res.message}"]
    if res.units_fixed:
        lines.append(f"已补 {len(res.units_fixed)} 个：{', '.join(res.units_fixed)}")
    if res.skipped:
        lines.append(f"⏭ 跳过 {len(res.skipped)} 个（非 Python / 改写不安全）")
    if res.emitter:
        receiver = "已检测到配置" if res.receiver_configured is True else (
            "未检测到配置" if res.receiver_configured is False else "未验证")
        lines.append(
            f"遥测状态：emitter=`{res.emitter}`；Receiver={receiver}；"
            f"delivery=`{res.delivery}`")
    if res.delivery_note:
        lines.append(res.delivery_note)
    lines.append(f"\n```diff\n{(res.diff or '(无 diff)')[:2500]}\n```")
    return "\n".join(lines)


def _handle_apply_reply(state: dict, msg: str) -> str:
    """处理用户对补埋点确认的回应：取消 / 确认 → 直接改文件 或 取消。"""
    prop = state.get("pending_apply") or {}
    state["pending_apply"] = None
    if _is_negative(msg):
        return "好的，已取消，不补埋点。"
    return _do_apply(prop.get("repo", ""))


def _agent_turn(state: dict, goal: str, context: str = "", rewrite_trace: Optional[dict] = None):
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

    try:
        run = _build_agent(broker).run(goal=goal, context=context)
    except Exception as exc:  # LLM 服务可能在启动后断开，不能让 Gradio 回调崩溃。
        base_url = getattr(_LLM.config, "base_url", "")
        return (
            "⚠️ 无法连接 LLM 服务，当前请求没有执行。\n\n"
            f"原因：`{type(exc).__name__}: {exc}`\n\n"
            f"请确认本地 Copilot API 代理正在运行：`{base_url}`。\n"
            "启动后可直接重新发送这条消息。",
            [],
            [],
        )
    run.rewrite_trace = rewrite_trace or {}
    _stash_focus_from_run(state, run)                    # GitHub/find_repo 也能切换会话焦点
    _stash_scan_from_run(state, run)                       # 记下本轮扫描盲区，供下一轮指代
    pending = _collect(run, "permission_required")
    if pending:
        matches = _all_matches(run)
        candidates = matches if len(matches) > 1 else []
        return _format_permission_request(pending, broker, candidates), pending, candidates
    denied = _collect(run, "denied")
    if denied:
        return _format_denied(denied, broker), [], []
    reply = _format_run(run)
    applied = _collect_apply_result(run)          # 补埋点执行了就附上 diff
    if applied:
        reply += "\n\n" + _format_applied(applied)
    return reply, [], []


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


def _message_text(content) -> str:
    """把 Gradio 6 的富文本 content 规范为普通文本，供 Agent 与状态机使用。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, (list, tuple)):
        return "\n".join(_message_text(item) for item in content).strip()
    return str(content or "")


def _ui_add_user(message, chat):
    """第一步（秒回）：立刻把用户消息显示到聊天区并清空输入框。"""
    chat = list(chat or [])
    msg = _message_text(message).strip()
    if msg:
        chat.append({"role": "user", "content": msg})
    return chat, ""


def _ui_bot(chat, state):
    """第二步（可能耗时）：读最后一条用户消息，跑 agent / 处理授权，补回复。"""
    logging.getLogger(__name__).info("sentinel: _ui_bot touches db")
    state = _ensure(state)
    chat = list(chat or [])
    if not chat or chat[-1].get("role") != "user":
        return chat, state, *_controls(state.get("pending"), state.get("candidates")), \
            *_ignore_controls(state)
    msg = _message_text(chat[-1].get("content")).strip()

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
        from sentinel.cognition.query_rewrite import render_rewrite_context, rewrite_query
        rewrite = rewrite_query(_LLM, msg, _focus_snapshot(state))
        ctx = "\n\n".join(part for part in (ctx, render_rewrite_context(rewrite)) if part)
        reply, pending, candidates = _agent_turn(
            state, msg, context=ctx, rewrite_trace=rewrite.to_dict())
        state["pending"] = pending
        state["candidates"] = candidates

    chat.append({"role": "assistant", "content": reply})
    return chat, state, *_controls(pending, candidates), *_ignore_controls(state)


def _ui_approve(chat, state, selected):
    state = _ensure(state)
    chat = list(chat or [])
    reply = _approve(state, selected)
    goal = _message_text(state.get("goal")).strip()
    chat.append({"role": "user", "content": f"✅ 同意扫描 `{selected or goal}`"})
    chat.append({"role": "assistant", "content": reply})
    return chat, state, *_controls([], []), *_ignore_controls(state)


def _ui_begin_approve(chat, state, selected):
    """按钮点击后立即隐藏授权控件，把耗时扫描留给后续回调。"""
    state = _ensure(state)
    chat = list(chat or [])
    target = selected or (state.get("pending") or [None])[0]
    state["approved_target"] = target
    state["pending"] = []
    state["candidates"] = []
    goal = _message_text(state.get("goal")).strip()
    chat.append({"role": "user", "content": f"✅ 同意扫描 `{target or goal}`"})
    return chat, state, *_controls([], [])


def _ui_finish_approve(chat, state):
    """授权控件隐藏后执行扫描，并把结果追加到对话。"""
    state = _ensure(state)
    chat = list(chat or [])
    target = state.pop("approved_target", None)
    chat.append({"role": "assistant", "content": _approve(state, target)})
    return chat, state, *_controls([], []), *_ignore_controls(state)


def _ui_deny(chat, state):
    state = _ensure(state)
    chat = list(chat or [])
    state["pending"] = []
    state["candidates"] = []
    chat.append({"role": "user", "content": "❌ 取消"})
    chat.append({"role": "assistant", "content": "好的，已取消，不读取该目录。"})
    return chat, state, *_controls([], []), *_ignore_controls(state)


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
    return chat, state, *_controls([], []), *_ignore_controls(state)


def build_demo() -> "gr.Blocks":
    """自定义 Blocks 界面：对话 + 授权按钮 + 多候选单选 + 结果可点击跳 VS Code。"""
    root = workspace_root()
    identity = LocalIdentity.from_env()
    _get_collaboration(identity)
    status = "✅ 已连接 LLM" if _LLM.available else "⚠️ 纯扫描模式（未配 LLM key）"

    with gr.Blocks(title="Sentinel · 可观测性守护 Agent") as demo:
        gr.Markdown(
            f"# 🛡️ Sentinel · 可观测性守护 Agent\n"
            f"读懂代码、找出监控盲区（调了依赖却没埋点的函数）。当前状态：{status}。\n"
            f"本地协作身份：`{identity.display_name}` (`{identity.user_id}`)；"
            f"工作区：`{identity.workspace_name}` (`{identity.workspace_id}`)。\n"
            f"允许范围：`{root}`。只给项目名也行，我会先找路径、再请你**授权**后扫描；"
            "结果里的文件名可**点击在 VS Code 打开**。"
        )
        state = gr.State({})
        # 用自适应高度替代固定 height：内容多时长到 max_height 再由外层滚动，
        # 避免长表格在气泡内形成嵌套滚动导致「滑不动、下面看不到」。resizable 让用户还能手动拖高。
        chatbot = gr.Chatbot(label="Sentinel", min_height=440, max_height=820, resizable=True)
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

        outs_btn = [chatbot, state, cand_radio, approve_btn, deny_btn, ignore_dd, ignore_btn]
        # 两步：先秒回显示用户消息+清空输入框，再 .then() 跑 agent 补回复。
        add_io = ([msg, chatbot], [chatbot, msg])
        send.click(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)
        msg.submit(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)

        approve_btn.click(
            _ui_begin_approve,
            [chatbot, state, cand_radio],
            [chatbot, state, cand_radio, approve_btn, deny_btn],
            api_name=False,
        ).then(_ui_finish_approve, [chatbot, state], outs_btn, api_name=False)
        deny_btn.click(_ui_deny, [chatbot, state], outs_btn, api_name=False)
        ignore_btn.click(_ui_ignore, [chatbot, state, ignore_dd], outs_btn, api_name=False)

    return demo


def run(host: str = "127.0.0.1", port: int = 7860) -> None:
    """启动：把 Gradio 挂在自建 FastAPI 上，额外提供 /open 端点（点文件名打开 VS Code）。"""
    import uvicorn
    from fastapi import FastAPI, Response
    from fastapi.responses import HTMLResponse

    app = FastAPI()

    @app.get("/open")
    def _open(path: str, line: int = 1):  # noqa: ANN202
        opened, message = _open_in_editor(path, line)
        if opened:
            return HTMLResponse(
                "<!doctype html><title>Opening file</title>"
                "<script>window.close()</script>"
                "<p>已请求在 VS Code 中打开文件；此临时页可以关闭。</p>"
            )
        return HTMLResponse(
            f"<p>无法打开文件：{message}</p><p><a href='/'>返回 Sentinel</a></p>",
            status_code=400,
        )

    @app.on_event("startup")
    def _warm_up() -> None:
        """启动预热：①自动注册内置语言(js/ts/tsx)（修「重启后 ts/tsx 又要重装」）；
        ②后台预热 embedder（首次会下载 ONNX 模型，同步做会卡住用户第一次点击）。
        """
        try:
            from sentinel.scanners.treesitter_scanner import register_builtin_languages
            register_builtin_languages()
        except Exception:  # noqa: BLE001
            pass

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
