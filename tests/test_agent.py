"""第 1 步：三范式 Agent 核心测试（离线，桩 LLM）。

分别验证：
  - plan()    Plan-and-Execute：解析有序 JSON 步骤（含围栏容忍、兜底）
  - act()     ReAct：真调工具、Finish 收尾、工具异常不崩、去重防死循环
  - reflect() Reflection：解析 verdict
  - run()     编排：达标即停 / 不达标重规划
运行：PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.agent import AgentCore, AgentRun, Tool  # noqa: E402


class _StubLLM:
    """按脚本顺序返回 .complete() 的结果（不联网）。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self._script.pop(0)


# -- 1) Plan ----------------------------------------------------------------

def test_plan_parses_ordered_json():
    llm = _StubLLM(['[{"tool": "echo", "why": "say hi"}, {"tool": "add", "why": "sum"}]'])
    core = AgentCore(llm)
    plan = core.plan("do something")
    assert [s["tool"] for s in plan] == ["echo", "add"]


def test_plan_tolerates_code_fences():
    llm = _StubLLM(['```json\n[{"tool": "echo", "why": "x"}]\n```'])
    core = AgentCore(llm)
    assert core.plan("g")[0]["tool"] == "echo"


def test_plan_falls_back_on_garbage():
    llm = _StubLLM(["not json"])
    core = AgentCore(llm)
    plan = core.plan("g")
    assert plan and "tool" in plan[0]


# -- 2) ReAct ---------------------------------------------------------------

def test_act_runs_tool_then_finishes():
    # 先调 add[2, 3]，观察到 5，再 Finish。
    llm = _StubLLM([
        "Thought: 我先算一下\nAction: add[2, 3]",
        "Thought: 得到 5，完成\nAction: Finish[结果是 5]",
    ])
    core = AgentCore(llm)
    run = AgentRun(goal="算 2+3")
    run.plan = [{"tool": "add", "why": "sum"}]
    core.act(run)
    assert run.answer == "结果是 5"
    actions = [t for t in run.transcript if t["type"] == "action"]
    assert actions[0]["tool"] == "add"
    assert actions[0]["observation"] == {"result": 5}


def test_act_tool_error_does_not_crash():
    # add 收到非法输入 → 结构化 error，循环继续，最后 Finish。
    llm = _StubLLM([
        "Thought: 试试\nAction: add[oops]",
        "Thought: 报错了，我直接结束\nAction: Finish[无法计算]",
    ])
    core = AgentCore(llm)
    run = AgentRun(goal="算点东西")
    run.plan = [{"tool": "add", "why": "sum"}]
    core.act(run)
    assert run.answer == "无法计算"
    # 失败被记录，但没崩
    assert run.failures and "error" in run.failures[0]
    action = [t for t in run.transcript if t["type"] == "action"][0]
    assert "error" in action["observation"]


def test_act_unknown_tool_is_structured_error():
    llm = _StubLLM([
        "Thought: 乱调\nAction: nosuchtool[x]",
        "Thought: 结束\nAction: Finish[done]",
    ])
    core = AgentCore(llm)
    run = AgentRun(goal="g")
    run.plan = []
    core.act(run)
    assert run.failures and "unknown tool" in run.failures[0]["error"]


def test_act_respects_max_steps():
    # 永远不 Finish，且每步换不同参数避免去重 → 靠 max_act_steps 兜底停下。
    script = [f"Thought: t{i}\nAction: echo[{i}]" for i in range(10)]
    llm = _StubLLM(script)
    core = AgentCore(llm, max_act_steps=3)
    run = AgentRun(goal="g")
    run.plan = []
    core.act(run)
    assert "上限" in run.answer or "limit" in run.answer
    assert llm.calls == 3  # 只跑了 max_act_steps 次


# -- 3) Reflection ----------------------------------------------------------

def test_reflect_parses_verdict():
    llm = _StubLLM(['{"score": 9, "complete": true, "missing": [], "next": ""}'])
    core = AgentCore(llm)
    run = AgentRun(goal="g")
    run.answer = "done"
    v = core.reflect(run)
    assert v["score"] == 9 and v["complete"] is True


def test_reflect_bad_json_treated_complete():
    llm = _StubLLM(["not json"])
    core = AgentCore(llm)
    run = AgentRun(goal="g")
    v = core.reflect(run)
    assert v["complete"] is True  # 防死循环


# -- 编排：Plan -> Act -> Reflect (-> 重规划) --------------------------------

def test_run_stops_when_good_enough():
    llm = _StubLLM([
        '[{"tool": "add", "why": "sum"}]',                          # plan
        "Thought: 算\nAction: add[2, 3]",                           # act step 1
        "Thought: 完成\nAction: Finish[5]",                         # act step 2
        '{"score": 9, "complete": true, "missing": [], "next": ""}',  # reflect
    ])
    core = AgentCore(llm, max_rounds=2)
    run = core.run("算 2+3")
    assert run.rounds == 1
    assert run.answer == "5"
    assert run.reflections[-1]["complete"] is True


def test_run_replans_then_stops():
    llm = _StubLLM([
        '[{"tool": "echo", "why": "hi"}]',                          # plan r1
        "Thought: echo\nAction: echo[hi]",                          # act r1 s1
        "Thought: done\nAction: Finish[hi]",                        # act r1 s2
        '{"score": 4, "complete": false, "missing": ["需要求和"], "next": "用 add 求和"}',  # reflect r1
        '[{"tool": "add", "why": "sum"}]',                          # replan r2
        "Thought: 求和\nAction: add[2, 3]",                         # act r2 s1
        "Thought: done\nAction: Finish[5]",                         # act r2 s2
        '{"score": 9, "complete": true, "missing": [], "next": ""}',  # reflect r2
    ])
    core = AgentCore(llm, max_rounds=3)
    run = core.run("先打招呼再算 2+3")
    assert run.rounds == 2
    assert len(run.reflections) == 2
    assert run.reflections[0]["complete"] is False
    assert run.reflections[1]["complete"] is True


def test_context_flows_into_plan_and_act():
    """会话背景应注入 plan 与 act 的系统提示，让多轮指代（如「把这个忽略」）能解析。"""
    seen = {}

    class CapLLM:
        available = True

        def complete(self, system, user):
            if "规划器" in system:
                seen["plan"] = system
                return '[{"tool": "echo", "why": "x"}]'
            if "执行器" in system:
                seen["act"] = system
                return "Action: Finish[done]"
            return '{"score": 9, "complete": true, "missing": [], "next": ""}'

    core = AgentCore(CapLLM())
    core.run(goal="把这个忽略", context="上次盲区: app.py::get_user [cache]")
    assert "app.py::get_user" in seen.get("plan", "")
    assert "app.py::get_user" in seen.get("act", "")

