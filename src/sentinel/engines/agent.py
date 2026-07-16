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

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# 反思达标线：>= 此分即“足够好，停止”。
_GOOD_ENOUGH = 8


# -- 工具抽象 --------------------------------------------------------------

@dataclass
class Tool:
    """一个工具：名字 + 说明 + 实现函数（吃一个字符串输入，返回任意结果）。"""
    name: str
    description: str
    func: Callable[[str], Any]


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
    '每步 {{"tool": "<名>", "why": "<理由>"}}，不要额外文字。\n\n'
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

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "plan": self.plan, "transcript": self.transcript,
            "reflections": self.reflections, "failures": self.failures,
            "answer": self.answer, "rounds": self.rounds,
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
                 max_rounds: int = 2, max_act_steps: int = 6):
        self.llm = llm
        self.tools = tools if tools is not None else _default_tools()
        self.max_rounds = max_rounds
        self.max_act_steps = max_act_steps

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
        except (ValueError, TypeError):
            pass
        # 兜底：一个安全的默认步（容错）。
        first = next(iter(self.tools), "echo")
        return [{"tool": first, "why": "fallback default step | 兜底默认步"}]

    # -- 2) ReAct -----------------------------------------------------------

    def act(self, run: AgentRun) -> None:
        """按计划跑 ReAct 循环；就地修改 run。手搓文本 ReAct，正则解析 Action。"""
        plan_text = "\n".join(
            f"{i+1}. [{s.get('tool','?')}] {s.get('why','')}"
            for i, s in enumerate(run.plan)
        ) or "(no plan)"
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
                # 的笔记/对话里，无需工具）。把它说的话当答案，而不是报「无法解析动作」。
                reply = (thought or _strip_speaker(text)).strip()
                run.answer = run.answer or reply or "（无法解析动作 | no action parsed）"
                if reply:
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
        """执行工具；异常结构化返回，绝不让其冒泡杀死循环（DESIGN §13）。"""
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool | 未知工具: {name}"}
        try:
            return {"result": tool.func(arg)}
        except Exception as e:  # noqa: BLE001 —— 有意兜住一切工具异常
            return {"error": f"{type(e).__name__}: {e}"}

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
        """从模型输出里解析 Thought 与 Action(tool[input])。"""
        thought_m = re.search(r"Thought:\s*(.*?)(?=\nAction:|$)", text or "", re.DOTALL)
        action_m = re.search(r"Action:\s*(\w+)\[(.*)\]", text or "", re.DOTALL)
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
        """一次完整自治运行：先 Plan，再 Act/Reflect 直到足够好。

        context：会话背景（近期对话 + 上次扫描结果），让“把这个忽略”这类多轮指代能解析。
        """
        run = AgentRun(goal=goal, context=context or "")
        run.plan = self.plan(goal, context=run.context)
        for round_i in range(1, self.max_rounds + 1):
            run.rounds = round_i
            self.act(run)
            verdict = self.reflect(run)
            run.reflections.append(verdict)
            if verdict.get("complete") or int(verdict.get("score", 0)) >= _GOOD_ENOUGH:
                break
            hint = verdict.get("next") or "; ".join(verdict.get("missing", []))
            if not hint:
                break
            run.plan = self.plan(goal, hint=hint, context=run.context)  # 带反思提示重规划
        return run
