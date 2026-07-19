"""三范式 Agent 核心（对应教材第 4 章）。

一个 AgentCore = 一个大脑、三个阶段（不是三个 Agent）：
  1. plan()    —— Plan-and-Execute：动手前先把目标拆成有序计划。
  2. act()     —— ReAct：Thought → Action → Observation 循环，真正调用工具。
  3. reflect() —— Reflection：行动后自评是否达成目标，不达标则重规划。

设计取舍：
- act() 用「手搓文本 ReAct」而非 function-calling —— 零依赖、任何模型可用、贴教材、看得见原理。
- 容错内建（DESIGN §13）：max_steps 防跑飞、工具异常结构化返回不崩、失败步不阻断整体。
- 只依赖 LLMClient.complete(system, user)，第 0 步就有，无需新依赖。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from sentinel.config import tool_call_log_path

# 反思达标线：>= 此分即“足够好，停止”。
_GOOD_ENOUGH = 8
_TOOL_LOG_LOCK = threading.Lock()
_TOOL_LOGGER = logging.getLogger("sentinel.tool_calls")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token)", re.IGNORECASE)


def _audit_value(value: Any, depth: int = 0) -> Any:
    """生成有大小上限的日志副本，并按字段名脱敏常见凭据。"""
    if depth >= 5:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if _SENSITIVE_KEY.search(str(key))
                       else _audit_value(item, depth + 1))
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple)):
        return [_audit_value(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return value if len(value) <= 4000 else value[:4000] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)[:4000]


def _write_tool_audit(event: Dict[str, Any]) -> None:
    """尽力写入一行审计日志；日志故障不能影响 Agent 主流程。"""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    try:
        path = tool_call_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with _TOOL_LOG_LOCK, open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:  # noqa: BLE001
        _TOOL_LOGGER.exception("failed to write tool audit log")


# -- 工具抽象 --------------------------------------------------------------

@dataclass
class Tool:
    """一个工具：名字 + 说明 + 实现函数（吃一个字符串输入，返回任意结果）。

    parameters：function calling 的 JSON schema（OpenAI 格式）；为 None 时用默认单字符串参数。
    """
    name: str
    description: str
    func: Callable[[str], Any]
    parameters: Optional[Dict[str, Any]] = None
    structured: bool = False           # True: 工具接收完整参数 dict（多参数）；否则收单字符串


def _default_tools() -> Dict[str, Tool]:
    """第 1 步的玩具工具，仅用于验证三范式循环本身（第 2 步换成真实领域工具）。"""

    def echo(text: str) -> str:
        return text

    def add(expr: str) -> int:
        # 输入形如 "2, 3" 或 "2 3"；解析两个整数求和。故意对非法输入抛错，演示容错。
        parts = re.split(r"[,\s]+", expr.strip())
        nums = [int(p) for p in parts if p != ""]
        if len(nums) != 2:
            raise ValueError(f"add 需要两个整数，收到：{expr!r}")
        return nums[0] + nums[1]

    return {
        "echo": Tool("echo", "原样返回输入文本 | echo back the input text", echo),
        "add": Tool("add", "把两个整数相加，输入形如 '2, 3' | add two integers", add),
    }


# -- Prompt（中英双语双版本，与用户共同确认 · DESIGN §15）----------------------

_PLAN_SYSTEM = (
    # EN: You are Sentinel's planner. Given a GOAL and the AVAILABLE TOOLS, produce a
    #     short ORDERED plan (2-5 steps) BEFORE any tool runs. Each step names ONE tool
    #     and why. Output ONLY a JSON list: [{"tool": "<name>", "why": "<reason>"}]. No prose.
    # ZH: 你是 Sentinel 的规划器。给定「目标」和「可用工具」，在动任何工具前先产出一个简短的
    #     有序计划（2-5 步）。每步指定一个工具及理由。只输出 JSON 列表：
    #     [{"tool": "<名>", "why": "<理由>"}]。不要额外文字。
    "You are Sentinel's planner / 你是 Sentinel 的规划器。\n"
    "Given a GOAL and AVAILABLE TOOLS, output ONLY a JSON list of 2-5 ordered steps, "
    'each {{"tool": "<name>", "why": "<reason>"}}. No prose.\n'
    "给定目标与可用工具，只输出 2-5 步的有序 JSON 列表，"
    '每步 {{"tool": "<名>", "why": "<理由>"}}，不要额外文字。\n'
    "If the GOAL is a conversational question answerable directly from CONTEXT "
    "(notes / recent conversation) with NO tool, output an EMPTY list [] . | "
    "若目标是可直接依据 CONTEXT（笔记/近期对话）回答、无需任何工具的对话式问题，"
    "输出空列表 []。\n\n"
    "CONTEXT (recent conversation & last scan) | 会话背景（近期对话与上次扫描）:\n{context}\n\n"
    "AVAILABLE TOOLS | 可用工具:\n{tools}"
)

_ACT_SYSTEM = (
    # EN: You are Sentinel's executor using ReAct. Follow the plan; think, then act with ONE
    #     tool at a time as `Action: tool_name[input]`. After observing, continue. When the
    #     goal is met, output `Action: Finish[final answer]`.
    # ZH: 你是 Sentinel 的执行器，采用 ReAct。按计划推进；先思考再一次调用一个工具，格式
    #     `Action: 工具名[输入]`。看到结果后继续。目标达成时输出 `Action: Finish[最终答案]`。
    "You are Sentinel's executor using the ReAct pattern / 你是 Sentinel 的执行器（ReAct）。\n"
    "Reply with EXACTLY one step:\n"
    "Thought: <your reasoning | 你的推理>\n"
    "Action: <tool_name>[<input>]   (use a real tool, or Finish[<answer>] when done)\n"
    "If the GOAL can be answered directly from CONTEXT (notes / recent conversation) WITHOUT any "
    "tool, output Action: Finish[answer] immediately and ignore the plan. | "
    "若目标能直接依据 CONTEXT（笔记 / 近期对话）回答、无需任何工具，"
    "立刻输出 Action: Finish[答案]，忽略计划。\n\n"
    "AVAILABLE TOOLS | 可用工具:\n{tools}\n\n"
    "GOAL | 目标:\n{goal}\n\n"
    "CONTEXT (recent conversation & last scan) | 会话背景（近期对话与上次扫描）:\n{context}\n\n"
    "PLAN | 计划:\n{plan}\n\n"
    "HISTORY | 历史:\n{history}"
)

_REFLECT_SYSTEM = (
    # EN: You are Sentinel's reviewer. Judge strictly whether the GOAL was achieved by the
    #     actions taken. Output ONLY JSON: {"score":<1-10>,"complete":<bool>,"missing":[...],
    #     "next":"<one concrete next step or empty>"}. No prose.
    # ZH: 你是 Sentinel 的评审。严格判断目标是否被已执行动作达成。只输出 JSON：
    #     {"score":<1-10>,"complete":<bool>,"missing":[...],"next":"<下一步或留空>"}。不要额外文字。
    "You are Sentinel's reviewer / 你是 Sentinel 的评审。\n"
    "Judge strictly whether the GOAL was achieved. Output ONLY JSON: "
    '{"score":<1-10>,"complete":<true|false>,"missing":["..."],"next":"<next step or empty>"}. '
    "No prose.\n严格判断目标是否达成，只输出上述 JSON，不要额外文字。"
)


# -- 运行结果 --------------------------------------------------------------

@dataclass
class AgentRun:
    """一次自治运行的完整轨迹 —— 未来驾驶舱视图的数据源。"""
    goal: str
    plan: List[Dict[str, str]] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    reflections: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)  # 容错：记录所有失败（DESIGN §13）
    answer: str = ""
    rounds: int = 0
    context: str = ""  # 会话背景（近期对话 + 上次扫描），让多轮指代能解析
    rewrite_trace: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "plan": self.plan, "transcript": self.transcript,
            "reflections": self.reflections, "failures": self.failures,
            "answer": self.answer, "rounds": self.rounds,
            "rewrite_trace": self.rewrite_trace,
        }


def _strip_fences(text: str) -> str:
    """容忍模型用 ``` 围栏包裹 JSON。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
        t = t.strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    return t


def _strip_speaker(text: str) -> str:
    """从模型的自由回复里剥掉 Thought:/Action: 前缀，取自然语言正文（对话式回答兜底用）。"""
    t = _strip_fences((text or "").strip())
    out: List[str] = []
    for ln in t.splitlines():
        s = ln.strip()
        if re.match(r"(?i)^\s*action\s*[:：]", s):
            continue
        s = re.sub(r"(?i)^\s*(thought|想)\s*[:：]\s*", "", s)
        if s:
            out.append(s)
    return " ".join(out).strip()


class AgentCore:
    """在工具之上编排 plan -> act(ReAct) -> reflect。

    llm 需提供 .complete(system, user) —— 第 0 步的 LLMClient 即满足。
    """

    def __init__(self, llm, tools: Optional[Dict[str, Tool]] = None,
                 max_rounds: int = 2, max_act_steps: int = 6, max_steps: int = 8):
        self.llm = llm
        self.tools = tools if tools is not None else _default_tools()
        self.max_rounds = max_rounds
        self.max_act_steps = max_act_steps
        self.max_steps = max_steps          # function-calling 循环的步数上限（防跑飞）

    def _tools_desc(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self.tools.values())

    # -- 1) Plan-and-Execute ------------------------------------------------

    def plan(self, goal: str, hint: str = "", context: str = "") -> List[Dict[str, str]]:
        """把目标拆成有序工具计划（此处不跑任何工具）。"""
        system = _PLAN_SYSTEM.format(tools=self._tools_desc(), context=context or "(无 | none)")
        user = goal if not hint else f"{goal}\n\n反思提示 | hint: {hint}"
        raw = self.llm.complete(system, user)
        try:
            plan = json.loads(_strip_fences(raw))
            if isinstance(plan, list) and all(isinstance(s, dict) for s in plan):
                return plan
            if isinstance(plan, list) and not plan:
                return []  # “无需工具，直接回答”（对话式问题）
        except (ValueError, TypeError):
            pass
        # 兜底：解析失败时也返回空计划而非硬塞 find_repo——
        # 后续 act 会走“直接从 CONTEXT 回答”路径，不把对话式问题拖去扫目录。
        return []

    # -- 2) ReAct -----------------------------------------------------------

    def act(self, run: AgentRun) -> None:
        """按计划跑 ReAct 循环；就地修改 run。手搓文本 ReAct，正则解析 Action。"""
        # 空计划 = 对话式问题直接从 CONTEXT 回答。不把 plan_text 拼成假步骤把模型带偏。
        if run.plan:
            plan_text = "\n".join(
                f"{i+1}. [{s.get('tool','?')}] {s.get('why','')}"
                for i, s in enumerate(run.plan)
            )
        else:
            plan_text = ("(空计划：目标可直接依据 CONTEXT 回答，无需工具 — 请直接输出 "
                         "Action: Finish[答案]) | (empty plan: answer from CONTEXT with Finish)")
        called: set = set()  # 去重防死循环（容错）

        for _ in range(self.max_act_steps):
            history = self._render_history(run)
            system = _ACT_SYSTEM.format(
                tools=self._tools_desc(), goal=run.goal, plan=plan_text, history=history,
                context=run.context or "(无 | none)")
            text = self.llm.complete(system, "继续 | continue")
            thought, action = self._parse(text)
            if thought:
                run.transcript.append({"type": "thought", "content": thought})

            if action is None:
                # 没有可执行动作：多半是模型在**直接对话回答**（如「你是谁」——答案就在 CONTEXT
                # 的笔记/对话里，无需工具），或在澄清（要扫哪个仓库）。把它说的话当答案回显，
                # 绝不崩成「无法解析动作」这种废话。
                reply = (thought or _strip_speaker(text) or (text or "").strip()).strip()
                if not reply:
                    reply = ("我不确定要操作哪个仓库，能说得更具体些吗？"
                             "例如「扫描 haulhero」或「重新扫 haulhero」。")
                run.answer = run.answer or reply
                run.transcript.append({"type": "finish", "content": reply})
                return

            name, arg = action
            if name.lower() == "finish":
                run.answer = arg
                run.transcript.append({"type": "finish", "content": arg})
                return

            key = f"{name}[{arg}]"
            if key in called:
                # 已调过同样的工具+参数 → 提示改用 Finish，避免死循环（容错）。
                run.transcript.append({"type": "action", "tool": name, "input": arg,
                                       "observation": {"note": "duplicate; use Finish | 重复调用，请 Finish"}})
                continue
            called.add(key)

            observation = self._run_tool(name, arg)
            run.transcript.append({"type": "action", "tool": name, "input": arg,
                                   "observation": observation})
            if "error" in observation:  # 失败步不阻断整体，只记录（容错）
                run.failures.append({"tool": name, "input": arg, "error": observation["error"]})

        run.answer = run.answer or "（达到行动步数上限 | reached act step limit）"

    def _run_tool(self, name: str, arg: str) -> Dict[str, Any]:
        """执行工具；持久记录入参、结果与 traceback，异常不杀死循环。"""
        call_id = uuid.uuid4().hex
        started_at = time.perf_counter()
        audited_input = _audit_value(arg)
        _write_tool_audit({
            "event": "tool_call_started",
            "call_id": call_id,
            "tool": name,
            "input": audited_input,
        })
        tool = self.tools.get(name)
        if tool is None:
            error = f"unknown tool | 未知工具: {name}"
            _write_tool_audit({
                "event": "tool_call_finished",
                "call_id": call_id,
                "tool": name,
                "status": "error",
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "error": error,
            })
            return {"error": error, "call_id": call_id}
        try:
            result = tool.func(arg)
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            business_error = result.get("error") if isinstance(result, dict) else None
            _write_tool_audit({
                "event": "tool_call_finished",
                "call_id": call_id,
                "tool": name,
                "status": "error" if business_error else "success",
                "duration_ms": duration_ms,
                "result": _audit_value(result),
                **({"error": str(business_error)} if business_error else {}),
            })
            if business_error:
                return {"error": str(business_error), "result": result, "call_id": call_id}
            return {"result": result}
        except Exception as e:  # noqa: BLE001 —— 有意兜住一切工具异常
            error = f"{type(e).__name__}: {e}"
            trace = traceback.format_exc()
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            _write_tool_audit({
                "event": "tool_call_finished",
                "call_id": call_id,
                "tool": name,
                "status": "exception",
                "duration_ms": duration_ms,
                "error": error,
                "traceback": trace,
            })
            _TOOL_LOGGER.error(
                "tool call failed call_id=%s tool=%s input=%r\n%s",
                call_id, name, audited_input, trace,
            )
            return {"error": error, "call_id": call_id}

    def _render_history(self, run: AgentRun) -> str:
        lines: List[str] = []
        for item in run.transcript:
            if item["type"] == "thought":
                lines.append(f"Thought: {item['content']}")
            elif item["type"] == "action":
                lines.append(f"Action: {item['tool']}[{item['input']}]")
                lines.append(f"Observation: {json.dumps(item['observation'], ensure_ascii=False)}")
        return "\n".join(lines) if lines else "(none yet | 暂无)"

    @staticmethod
    def _parse(text: str):
        """从模型输出里解析 Thought 与 Action。

        容错：ReAct 约定是 `Action: tool[input]`（方括号），但模型常被工具描述里的
        `tool(input)` 函数风格带偏而写成圆括号——两种都接受，避免因括号类型丢动作。
        """
        t = text or ""
        thought_m = re.search(r"Thought:\s*(.*?)(?=\nAction:|$)", t, re.DOTALL)
        action_m = (re.search(r"Action:\s*(\w+)\s*\[(.*)\]", t, re.DOTALL)
                    or re.search(r"Action:\s*(\w+)\s*\((.*)\)", t, re.DOTALL))
        thought = thought_m.group(1).strip() if thought_m else None
        action = (action_m.group(1).strip(), action_m.group(2).strip()) if action_m else None
        return thought, action

    # -- 3) Reflection ------------------------------------------------------

    def reflect(self, run: AgentRun) -> Dict[str, Any]:
        """自评目标是否达成；返回结构化判断。"""
        actions = [
            {"tool": t.get("tool"), "input": t.get("input"), "observation": t.get("observation")}
            for t in run.transcript if t.get("type") == "action"
        ]
        user = (
            f"GOAL | 目标:\n{run.goal}\n\n"
            f"ACTIONS | 已执行动作:\n{json.dumps(actions, ensure_ascii=False, indent=2)}\n\n"
            f"ANSWER | 最终答案:\n{run.answer}"
        )
        raw = self.llm.complete(_REFLECT_SYSTEM, user)
        try:
            verdict = json.loads(_strip_fences(raw))
            if isinstance(verdict, dict):
                verdict.setdefault("score", 7)
                verdict.setdefault("complete", bool(int(verdict.get("score", 0)) >= _GOOD_ENOUGH))
                verdict.setdefault("missing", [])
                verdict.setdefault("next", "")
                return verdict
        except (ValueError, TypeError):
            pass
        # 解析失败 → 视为完成，避免死循环（容错）。
        return {"score": 7, "complete": True, "missing": [], "next": ""}

    # -- 编排 ---------------------------------------------------------------

    def run(self, goal: str, context: str = "") -> AgentRun:
        """一次完整自治运行：**function calling 循环**（业界成熟做法）。

        模型看[对话 + 工具清单] → 原生返回结构化 tool_calls 或最终答案；
        有工具调用就执行→回喂→继续；没有就结束。不再手搜文本 ReAct + 正则解析，
        从根上消除格式/误判脆弱性（园括号/复述不执行那类 bug）。
        context：会话背景（近期对话 + 上次扫描），让多轮指代能解析。
        """
        run = AgentRun(goal=goal, context=context or "")
        messages = [
            {"role": "system", "content": self._system(run.context)},
            {"role": "user", "content": goal},
        ]
        specs = self._tool_specs()
        for _ in range(self.max_steps):
            msg = self.llm.chat(messages, tools=specs)
            tool_calls = getattr(msg, "tool_calls", None) or []
            if msg.content:
                run.transcript.append({"type": "thought", "content": msg.content})
            if not tool_calls:
                run.answer = (msg.content or "").strip() or "（无回复）"
                run.transcript.append({"type": "finish", "content": run.answer})
                self._maybe_reflect(run)
                return run
            # 把 assistant 消息（含 tool_calls）回写历史，再逐个执行工具。
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (ValueError, TypeError):
                    args = {}
                tool = self.tools.get(name)
                if tool is not None and getattr(tool, "structured", False):
                    call_arg = args                    # 结构化工具：传完整参数 dict
                    disp = json.dumps(args, ensure_ascii=False)
                else:
                    call_arg = args.get("input")
                    if call_arg is None:               # 兼容语义化参数名（path/query/branch...）
                        call_arg = next((v for v in args.values()), "")
                    call_arg = str(call_arg)
                    disp = call_arg
                observation = self._run_tool(name, call_arg)
                run.transcript.append({"type": "action", "tool": name,
                                       "input": disp, "observation": observation})
                if "error" in observation:
                    run.failures.append({"tool": name, "input": disp,
                                         "error": observation["error"]})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(observation, ensure_ascii=False)})
                result = observation.get("result") if isinstance(observation, dict) else None
                applied = result.get("applied") if isinstance(result, dict) else None
                if isinstance(applied, dict):
                    run.answer = applied.get("message") or "补埋点已完成，改动等待审阅。"
                    run.transcript.append({"type": "finish", "content": run.answer})
                    self._maybe_reflect(run)
                    return run
        run.answer = run.answer or "（达到步数上限 | reached step limit）"
        run.transcript.append({"type": "finish", "content": run.answer})
        self._maybe_reflect(run)
        return run

    def _system(self, context: str) -> str:
        """系统提示：身份 + 行为原则 + 会话背景。不需 ReAct 格式说明（function calling 自动处理）。"""
        return (
            "你是 Sentinel，一个面向多人协作代码库的可观测性守护 Agent。\n"
            "你可以调用工具来查找项目、扫描监控盲区、补埋点、记/查笔记等。\n"
            "原则：\n"
            "- 用户表达了**执行意图**（扫描/补埋点/查找等）——哪怕夹着疑问或抱怨——就**直接调用对应工具**，不要只复述用户意图。\n"
            "- 用户明确要求补/修上次扫描出的盲区时，必须调用 apply_instrumentation；"
            "只能在工具返回真实失败时解释原因，不能臆测某种语言不支持。\n"
            "- 发现 unknown 扩展名时，先用 check_language_support 报告事实；用户确认 language 与 extensions 后，"
            "调用 register_dynamic_language_support，再重扫。不得凭扩展名臆造语言映射。\n"
            "- 纯对话/能直接从背景回答的，就直接回答，不必硬调工具。\n"
            "- 调工具时用绝对路径，不臆造不存在的路径；指代（这个/那个/再扫一遍）结合背景消解。\n\n"
            f"会话背景 CONTEXT：\n{context or '（无）'}"
        )

    def _tool_specs(self):
        """把工具集转成 OpenAI function calling 的 tools schema。"""
        specs = []
        for t in self.tools.values():
            params = t.parameters or {
                "type": "object",
                "properties": {"input": {"type": "string",
                                         "description": "工具输入（如路径/关键词/分支名）"}},
                "required": ["input"],
            }
            specs.append({"type": "function",
                          "function": {"name": t.name, "description": t.description,
                                       "parameters": params}})
        return specs

    def _maybe_reflect(self, run: AgentRun) -> None:
        """只对「有工具调用」的运行做一次反思（纯对话不反思，省调用）；失败不阻断。"""
        if not any(t.get("type") == "action" for t in run.transcript):
            return
        run.rounds = 1
        try:
            run.reflections.append(self.reflect(run))
        except Exception:  # noqa: BLE001
            pass
