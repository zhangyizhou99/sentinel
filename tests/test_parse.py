"""ReAct 动作解析容错测试（方括号 / 圆括号都要能解析）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.agent import AgentCore  # noqa: E402


def test_parse_square_brackets():
    assert AgentCore._parse("Action: scan[/a/b/c]")[1] == ("scan", "/a/b/c")


def test_parse_round_brackets():
    # 模型常被工具描述的 tool(input) 风格带偏而写圆括号，也要能解析（否则动作丢失）
    assert AgentCore._parse("Action: scan(/a/b/c)")[1] == ("scan", "/a/b/c")


def test_parse_finish_and_thought():
    thought, action = AgentCore._parse("Thought: 好了\nAction: Finish[答案]")
    assert thought == "好了"
    assert action == ("Finish", "答案")


def test_parse_no_action():
    assert AgentCore._parse("我直接回答你")[1] is None
