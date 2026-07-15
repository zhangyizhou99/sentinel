"""Sentinel 对话式 Web 界面（Gradio ChatInterface）。

用户在对话里下达目标（如「扫一下 /path/to/repo」），交给三范式 AgentCore 自治执行：
    plan → act(ReAct，真实调用 scan 工具) → reflect
配了 LLM key 时是真正的 agent 推理；没配 key 时降级为「纯扫描直报」，仍可用。

运行：PYTHONPATH=src python3 -m sentinel.webapp
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

import gradio as gr

from sentinel.config import workspace_root
from sentinel.engines.agent import AgentCore, AgentRun
from sentinel.engines.agent_tools import build_find_repo_tool, build_scan_tool
from sentinel.engines.scan import scan_repo, signals_of
from sentinel.llm import LLMClient
from sentinel.permissions import PermissionBroker

# 全局单例：LLM 客户端（无 key 时 available=False，走降级路径，不崩）。
_LLM = LLMClient()


def _build_agent(broker: PermissionBroker) -> AgentCore:
    """构造带 find_repo + scan 两个工具的 agent（都受该会话的权限门约束）。"""
    find = build_find_repo_tool(broker)
    scan = build_scan_tool(broker)
    return AgentCore(_LLM, tools={find.name: find, scan.name: scan})


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


def _vscode_link(repo: str, rel_file: str, lines: str) -> str:
    """拼一个 VS Code 深链：点击直接在编辑器里跳到该文件对应行。"""
    abspath = rel_file if os.path.isabs(rel_file) else os.path.join(repo or "", rel_file)
    start = str(lines or "1").split("-")[0].strip() or "1"
    return f"vscode://file{abspath}:{start}"


def _render_report(data: dict) -> str:
    """把一份 scan 报告渲染成易读表格；文件名做成可点击的 VS Code 深链。"""
    spots = data.get("blind_spots", [])
    repo = data.get("repo", "")
    head = (f"扫描 `{repo or '?'}`：共 **{data.get('total_units', '?')}** 个函数，"
            f"发现 **{data.get('blind_spot_count', len(spots))}** 个监控盲区。")
    if not spots:
        return head + "\n\n✅ 没有明显盲区。"
    rows = ["| 文件（点击在 VS Code 打开） | 函数 | 风险信号 | 行 |", "| --- | --- | --- | --- |"]
    for b in spots:
        sig = ", ".join(b.get("signals", [])) or "-"
        link = _vscode_link(repo, b.get("file", ""), b.get("lines", ""))
        file_cell = f"[{b.get('file', '?')}]({link})"
        rows.append(f"| {file_cell} | `{b.get('function', '?')}` | {sig} | {b.get('lines', '?')} |")
    return head + "\n\n" + "\n".join(rows)


def _format_run(run: AgentRun) -> str:
    """把一次 agent 自治运行渲染成对话回复：结论 + 可展开的过程。"""
    parts: List[str] = []

    # 1) 事实层：用工具真实观测渲染盲区表（ground truth）。
    reports = _scan_reports(run)
    if reports:
        parts.append("\n\n".join(_render_report(r) for r in reports))

    # 2) 解读层：LLM 的自然语言结论，仅当它是散文（非 JSON 改写）才展示，避免与表格重复。
    ans = (run.answer or "").strip()
    if ans and not ans.startswith("{"):
        parts.append("💬 " + ans)
    elif not reports:
        parts.append(ans or "（没有得到结论）")

    # 过程：计划 + 每次工具调用的观测（透明可审计）。
    trace_lines: List[str] = []
    if run.plan:
        steps = " → ".join(f"{s.get('tool', '?')}" for s in run.plan)
        trace_lines.append(f"**计划**：{steps}")
    for item in run.transcript:
        if item.get("type") == "action":
            obs = item.get("observation", {})
            if "error" in obs:
                trace_lines.append(f"- `{item['tool']}[{item['input']}]` → ⚠️ {obs['error']}")
            else:
                trace_lines.append(f"- `{item['tool']}[{item['input']}]` ✓")

    if trace_lines:
        body = "\n".join(trace_lines)
        parts.append(f"\n<details><summary>🔎 过程（{run.rounds} 轮）</summary>\n\n{body}\n</details>")
    return "\n".join(parts)


def _format_scan_result(path: str) -> str:
    """降级模式：不经 LLM，直接扫描并渲染盲区表（文件名可点击跳 VS Code）。"""
    result = scan_repo(path)
    spots = result.blind_spots
    head = f"扫描 `{path}`：共 **{len(result.units)}** 个函数，发现 **{len(spots)}** 个监控盲区。"
    if not spots:
        return head + "\n\n✅ 没有明显盲区。"
    rows = ["| 文件（点击在 VS Code 打开） | 函数 | 风险信号 | 行 |", "| --- | --- | --- | --- |"]
    for u in spots[:30]:
        link = _vscode_link(path, u.file, f"{u.start_line}-{u.end_line}")
        file_cell = f"[{u.file}]({link})"
        rows.append(f"| {file_cell} | `{u.qualname}` | {', '.join(signals_of(u)) or '-'} | {u.start_line}-{u.end_line} |")
    return head + "\n\n" + "\n".join(rows)


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


def _agent_turn(state: dict, goal: str):
    """跑一次 agent，返回 (回复文本, 待授权路径, 候选路径)。"""
    broker: PermissionBroker = state["broker"]
    state["goal"] = goal

    if not _LLM.available:  # 降级：无 LLM，能认出 scope 内路径就直接扫
        path = _extract_path(goal)
        note = f"_（无 LLM key，纯扫描模式：{_LLM.why_unavailable()}）_\n\n"
        if path and broker.within_scope(path):
            return note + _format_scan_result(path), [], []
        if path:
            return note + f"⚠️ `{path}` 超出允许范围（{broker.root}）。", [], []
        return note + f"给我一个（{broker.root} 内的）仓库路径或项目名。", [], []

    run = _build_agent(broker).run(goal=goal)
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
    report = build_scan_tool(broker).func(target)
    if isinstance(report, dict) and "blind_spots" in report:
        return _render_report(report)
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
        return chat, *_controls(state.get("pending"), state.get("candidates"))
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
        reply, pending, candidates = _agent_turn(state, msg)
        state["pending"] = pending
        state["candidates"] = candidates

    chat.append({"role": "assistant", "content": reply})
    return chat, *_controls(pending, candidates)


def _ui_approve(chat, state, selected):
    state = _ensure(state)
    chat = list(chat or [])
    reply = _approve(state, selected)
    chat.append({"role": "user", "content": f"✅ 同意扫描 `{selected or (state.get('goal') or '')}`"})
    chat.append({"role": "assistant", "content": reply})
    return chat, *_controls([], [])


def _ui_deny(chat, state):
    state = _ensure(state)
    chat = list(chat or [])
    state["pending"] = []
    state["candidates"] = []
    chat.append({"role": "user", "content": "❌ 取消"})
    chat.append({"role": "assistant", "content": "好的，已取消，不读取该目录。"})
    return chat, *_controls([], [])


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
            msg = gr.Textbox(placeholder="扫一下 sentinel-sample-app", show_label=False, scale=8, autofocus=True)
            send = gr.Button("发送", variant="primary", scale=1)

        outs_btn = [chatbot, cand_radio, approve_btn, deny_btn]
        # 两步：先秒回显示用户消息+清空输入框，再 .then() 跑 agent 补回复。
        add_io = ([msg, chatbot], [chatbot, msg])
        send.click(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)
        msg.submit(_ui_add_user, *add_io, api_name=False).then(
            _ui_bot, [chatbot, state], outs_btn, api_name=False)

        approve_btn.click(_ui_approve, [chatbot, state, cand_radio], outs_btn, api_name=False)
        deny_btn.click(_ui_deny, [chatbot, state], outs_btn, api_name=False)

    return demo


if __name__ == "__main__":
    build_demo().launch(show_api=False)
