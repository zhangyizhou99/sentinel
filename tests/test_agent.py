"""第 1 步：三范式 Agent 核心测试（离线，桩 LLM）。

分别验证：
  - plan()    Plan-and-Execute：解析有序 JSON 步骤（含围栏容忍、兜底）
  - act()     ReAct：真调工具、Finish 收尾、工具异常不崩、去重防死循环
  - reflect() Reflection：解析 verdict
  - run()     编排：达标即停 / 不达标重规划
运行：PYTHONPATH=src pytest tests/ -q
"""
import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.agent import AgentCore, AgentRun, Tool  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_tool_call_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_TOOL_CALL_LOG", str(tmp_path / "tool-calls.jsonl"))


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
    """解析失败 → 兜底为空计划（不再硬塞 find_repo），交给 act 直接依上下文回答。"""
    llm = _StubLLM(["not json"])
    core = AgentCore(llm)
    plan = core.plan("g")
    assert plan == []


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


# -- 编排：function calling 循环 --------------------------------------------


class _Msg:
    """模拟 openai message 对象（含 content 与 tool_calls）。"""
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def _tc(name, arguments, cid="c1"):
    fn = type("F", (), {"name": name, "arguments": arguments})()
    return type("TC", (), {"id": cid, "function": fn})()


class _ChatStub:
    """桩 LLM：chat() 按脚本返回 message；complete() 供 reflect 用。"""
    available = True

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return self._script.pop(0)

    def complete(self, system, user):
        return '{"score": 9, "complete": true, "missing": [], "next": ""}'


def test_run_calls_tool_then_answers():
    llm = _ChatStub([_Msg(tool_calls=[_tc("echo", '{"input": "hi"}')]),
                     _Msg(content="完成了")])
    core = AgentCore(llm, tools={"echo": Tool("echo", "回显", lambda s: s)})
    run = core.run("echo hi")
    assert run.answer == "完成了"
    actions = [t for t in run.transcript if t["type"] == "action"]
    assert actions[0]["tool"] == "echo"
    assert actions[0]["observation"] == {"result": "hi"}
    assert run.reflections and run.reflections[-1]["complete"] is True   # 有工具调用→反思


def test_run_direct_answer_needs_no_tool():
    llm = _ChatStub([_Msg(content="你是 jiojio，本项目负责人。")])
    core = AgentCore(llm, tools={})
    run = core.run("我是谁", context="[NOTES] 我是负责人jiojio")
    assert "jiojio" in run.answer
    assert run.reflections == []                        # 纯对话不反思
    assert "无法解析动作" not in run.answer


def test_run_tool_error_recorded_not_crash():
    def boom(_):
        raise ValueError("坏了")
    llm = _ChatStub([_Msg(tool_calls=[_tc("boom", '{"input": "x"}')]),
                     _Msg(content="出错了但我没崩")])
    core = AgentCore(llm, tools={"boom": Tool("boom", "会抛错", boom)})
    run = core.run("试试")
    assert run.answer == "出错了但我没崩"
    assert run.failures and "error" in run.failures[0]


def test_tool_exception_writes_traceback_and_redacts_secrets(tmp_path, monkeypatch):
    log_path = tmp_path / "tool-calls.jsonl"
    monkeypatch.setenv("SENTINEL_TOOL_CALL_LOG", str(log_path))

    def boom(_):
        raise TypeError("'NoneType' object is not subscriptable")

    core = AgentCore(_StubLLM([]), tools={"boom": Tool("boom", "会抛错", boom)})
    observation = core._run_tool("boom", {"repo": "d:/Code/app", "token": "secret"})

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert observation["call_id"] == records[-1]["call_id"]
    assert records[0]["input"]["token"] == "<redacted>"
    assert records[-1]["status"] == "exception"
    assert "Traceback (most recent call last)" in records[-1]["traceback"]
    assert "NoneType" in records[-1]["traceback"]


def test_tool_business_error_is_recorded_as_failure(tmp_path, monkeypatch):
    log_path = tmp_path / "tool-calls.jsonl"
    monkeypatch.setenv("SENTINEL_TOOL_CALL_LOG", str(log_path))
    core = AgentCore(
        _StubLLM([]),
        tools={"reject": Tool("reject", "返回业务错误", lambda _: {"error": "没找到盲区"})},
    )

    observation = core._run_tool("reject", "queue.ts")

    assert observation["error"] == "没找到盲区"
    assert observation["result"] == {"error": "没找到盲区"}
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["status"] == "error"


def test_run_stops_after_successful_apply_without_model_reinterpretation():
    llm = _ChatStub([
        _Msg(tool_calls=[_tc("apply_instrumentation", '{"input": "queue.ts"}')]),
        _Msg(content="错误地说没有匹配项"),
    ])
    tool = Tool(
        "apply_instrumentation",
        "补埋点",
        lambda _: {"applied": {"message": "已补 4 个埋点", "units_fixed": ["a", "b", "c", "d"]}},
    )

    run = AgentCore(llm, tools={tool.name: tool}).run("补 queue.ts")

    assert run.answer == "已补 4 个埋点"
    assert llm.calls == 1


def test_run_respects_max_steps():
    # 永远返回 tool_calls → 靠 max_steps 兜底停下。
    script = [_Msg(tool_calls=[_tc("echo", f'{{"input": "{i}"}}')]) for i in range(20)]
    core = AgentCore(_ChatStub(script), tools={"echo": Tool("echo", "e", lambda s: s)},
                     max_steps=3)
    run = core.run("停不下来")
    assert "上限" in run.answer or "limit" in run.answer


def test_run_semantic_arg_name():
    # 工具用语义化参数名（path 而非 input）也能取到。
    captured = {}

    def scan(p):
        captured["p"] = p
        return {"ok": p}

    tool = Tool("scan", "扫描", scan,
                parameters={"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"]})
    llm = _ChatStub([_Msg(tool_calls=[_tc("scan", '{"path": "/a/b"}')]),
                     _Msg(content="扫完了")])
    AgentCore(llm, tools={"scan": tool}).run("扫 /a/b")
    assert captured["p"] == "/a/b"


def test_context_flows_into_system():
    seen = {}

    class Cap:
        available = True

        def chat(self, messages, tools=None):
            seen["sys"] = messages[0]["content"]
            return _Msg(content="ok")

        def complete(self, system, user):
            return '{"score":9,"complete":true,"missing":[],"next":""}'

    AgentCore(Cap()).run(goal="把这个忽略", context="上次盲区: app.py::get_user [cache]")
    assert "app.py::get_user" in seen.get("sys", "")


