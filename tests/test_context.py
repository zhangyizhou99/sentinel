"""笔记库 + 上下文构建器测试。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.memory import NoteStore  # noqa: E402
from sentinel.model.code_unit import CodeUnit  # noqa: E402
from sentinel.cognition.context_builder import (  # noqa: E402
    ContextBuilder, ContextTarget, ContextSection, EvidenceProvider,
    TargetProvider, NoteProvider, KnowledgeProvider, HistoryProvider,
    default_judge_builder, estimate_tokens,
)


def _notes() -> NoteStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return NoteStore(db_path=path)


def _unit(qualname="create_order", calls=("redis.get", "httpx.post")) -> CodeUnit:
    return CodeUnit(file="app.py", qualname=qualname, kind="function",
                    signature="(self)", docstring="", calls=list(calls),
                    start_line=1, end_line=9, has_instrumentation=False)


# ---- NoteStore ----------------------------------------------------------

def test_note_add_and_scope():
    ns = _notes()
    gid = ns.add_note("全局约定：外部调用都要埋点")                 # global
    rid = ns.add_note("本仓库用 pino 打日志", repo="/tmp/r")       # repo
    uid = ns.add_note("这个函数故意不埋点", repo="/tmp/r",
                      unit_id="app.py::create_order", tags=["cache"])  # unit
    assert gid and rid and uid
    all_notes = {n.id: n for n in ns.list_notes("/tmp/r")}
    assert all_notes[gid].scope == "global"
    assert all_notes[rid].scope == "repo"
    assert all_notes[uid].scope == "unit"
    ns.close()


def test_note_search_ranking():
    ns = _notes()
    ns.add_note("全局：谨慎埋点")                                   # global, base
    ns.add_note("仓库约定", repo="/tmp/r")                         # repo +40
    ns.add_note("这个函数别埋", repo="/tmp/r",
                unit_id="app.py::create_order", tags=["cache"])    # unit +100(+tags)
    hits = ns.search_notes(repo="/tmp/r", unit_id="app.py::create_order",
                           tags=["cache"], limit=5)
    assert hits[0].note.scope == "unit"                            # 最相关排最前
    # 别的仓库的笔记不串进来
    other = ns.search_notes(repo="/tmp/other", unit_id="", tags=[], query="")
    assert all(h.note.repo in ("", os.path.abspath("/tmp/other")) for h in other)
    ns.close()


def test_note_search_skips_other_unit_specific():
    ns = _notes()
    ns.add_note("给 f 的专属笔记", repo="/tmp/r", unit_id="app.py::f")
    hits = ns.search_notes(repo="/tmp/r", unit_id="app.py::g", tags=[], query="")
    assert hits == []                                             # 别的函数的专属笔记不出现
    ns.close()


# ---- ContextBuilder -----------------------------------------------------

def test_target_always_present():
    b = ContextBuilder([TargetProvider()], token_budget=1000)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    assert "[FUNCTION]" in ctx.text
    assert any(s.source == "target" for s in ctx.sections)


def test_knowledge_provider_and_refs():
    b = ContextBuilder([TargetProvider(), KnowledgeProvider()], token_budget=1000)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache", "http"]))
    refs = ctx.refs()
    assert "knowledge:cache" in refs and "knowledge:http" in refs


def test_notes_injected_into_context():
    ns = _notes()
    ns.add_note("团队约定：cache 调用要打命中率", repo="/tmp/r", tags=["cache"])
    b = ContextBuilder([TargetProvider(), NoteProvider(ns)], token_budget=1000)
    ctx = b.build(ContextTarget(unit=_unit(), repo="/tmp/r", signals=["cache"]))
    assert "[NOTES]" in ctx.text
    assert any(s.source == "note" for s in ctx.sections)
    ns.close()


def test_budget_drops_low_priority():
    # 预算很小：只保住 target（最高优先级·必留），knowledge 被丢弃。
    b = ContextBuilder([TargetProvider(), KnowledgeProvider()], token_budget=8)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache", "http", "db"]))
    assert any(s.source == "target" and s.included for s in ctx.sections)
    assert ctx.dropped                                            # 有被预算丢弃的
    # target 优先级最高（必留）；knowledge 被挤掉
    assert all(s.source != "knowledge" for s in ctx.sections)
    assert all(s.source == "knowledge" for s in ctx.dropped)


def test_provider_failure_is_isolated():
    class Boom(EvidenceProvider):
        def provide(self, target):
            raise RuntimeError("boom")
    b = ContextBuilder([TargetProvider(), Boom()], token_budget=1000)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    assert any(s.source == "target" for s in ctx.sections)        # 一个源炸了不影响整体


def test_estimate_tokens_monotonic():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 40) >= estimate_tokens("a" * 4)


class _Fake(EvidenceProvider):
    """产出预设片段的假证据源（测压缩用）。"""
    def __init__(self, sections):
        self._s = sections
    def provide(self, target):
        return list(self._s)


def test_dedup_collapses_identical_sections():
    dup = ContextSection("knowledge", "cache", "- knowledge:cache", priority=55,
                         ref="knowledge:cache")
    dup2 = ContextSection("knowledge", "cache", "- knowledge:cache", priority=55,
                          ref="knowledge:cache")
    b = ContextBuilder([TargetProvider(), _Fake([dup, dup2])], token_budget=1000)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    kn = [s for s in ctx.sections if s.source == "knowledge"]
    assert len(kn) == 1                                          # 重复的只留一条


def test_degrades_to_compact_before_dropping():
    big = ContextSection("note", "n", "N" * 400, priority=50, ref="note:1",
                         compact="精简版")
    # 预算只够 target + 一点点：note 放不下全文，但能放下 compact
    b = ContextBuilder([TargetProvider(), _Fake([big])], token_budget=40)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    note = next(s for s in ctx.sections if s.source == "note")
    assert note.level == "compact"                              # 降级而非丢弃
    assert "精简版" in ctx.text


def test_degrades_to_clipped_when_compact_too_big():
    big = ContextSection("note", "n", "N" * 400, priority=50, ref="note:1",
                         compact="Y" * 240, min_chars=40)
    b = ContextBuilder([TargetProvider(), _Fake([big])], token_budget=55)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    note = next((s for s in ctx.sections if s.source == "note"), None)
    assert note is not None and note.level == "clipped"        # compact 也放不下 → 截断


def test_optional_llm_compressor_hook():
    big = ContextSection("note", "n", "N" * 4000, priority=50, ref="note:1")
    calls = {"n": 0}
    def fake_compressor(text, target_tokens):
        calls["n"] += 1
        return "SUMMARY"
    b = ContextBuilder([TargetProvider(), _Fake([big])], token_budget=60,
                       compressor=fake_compressor)
    ctx = b.build(ContextTarget(unit=_unit(), signals=["cache"]))
    note = next((s for s in ctx.sections if s.source == "note"), None)
    assert calls["n"] == 1 and note and note.level == "summarized"
    assert "SUMMARY" in ctx.text

