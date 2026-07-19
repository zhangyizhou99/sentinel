"""LLM query rewrite：短语义补全必须受会话事实约束。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.cognition.query_rewrite import render_rewrite_context, rewrite_query  # noqa: E402


class _RewriteLLM:
    available = True

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.response


def _last_scan():
    return {
        "repo": "d:/Code/haulhero-frontend",
        "language_gap": {"typescript": 6, "tsx": 8},
        "spots": [],
    }


def test_rewrite_expands_short_query_from_actual_language_gap():
    llm = _RewriteLLM(
        '{"rewritten_goal":"安装 TS/TSX 解析支持后重新扫描",'
        '"candidate_actions":["install_language_support","scan"],'
        '"repo":"d:/Code/haulhero-frontend","languages":["typescript","tsx"],'
        '"reason":"使用上次扫描的缺口"}'
    )
    trace = rewrite_query(llm, "补齐后再扫一下", _last_scan())

    assert trace.validated["repo"] == "d:/Code/haulhero-frontend"
    assert trace.validated["languages"] == ["typescript", "tsx"]
    assert trace.validated["candidate_actions"] == ["install_language_support", "scan"]
    context = render_rewrite_context(trace)
    assert "install_language_support 仍需用户明确同意" in context
    assert "typescript" in llm.calls[0][1]


def test_rewrite_removes_fabricated_facts_and_actions():
    llm = _RewriteLLM(
        '{"rewritten_goal":"扫描 secret",'
        '"candidate_actions":["delete_everything","scan"],'
        '"repo":"d:/secret","languages":["rust","tsx"]}'
    )
    trace = rewrite_query(llm, "处理一下", _last_scan())

    assert trace.validated["repo"] == "d:/Code/haulhero-frontend"
    assert trace.validated["languages"] == ["tsx"]
    assert trace.validated["candidate_actions"] == ["scan"]
    assert any("非事实仓库" in note for note in trace.validation_notes)
    assert any("rust" in note for note in trace.validation_notes)
    assert any("delete_everything" in note for note in trace.validation_notes)


def test_rewrite_without_llm_uses_deterministic_draft():
    class _Offline:
        available = False

    trace = rewrite_query(_Offline(), "补齐后再扫一下", _last_scan())

    assert set(trace.validated["languages"]) == {"typescript", "tsx"}
    assert trace.validated["candidate_actions"] == ["install_language_support", "scan"]
    assert "LLM 不可用" in trace.validation_notes[0]