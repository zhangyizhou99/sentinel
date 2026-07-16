"""意图判定 judge_intent 测试（桩 LLM，不联网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.model.code_unit import CodeUnit  # noqa: E402
from sentinel.engines.judge import judge_intent, _peers  # noqa: E402
from sentinel.engines.knowledge import knowledge_for  # noqa: E402
from sentinel.cognition import CodeIndex, HashEmbedder, MemoryStore  # noqa: E402


class StubLLM:
    def __init__(self, available=True, response=""):
        self.available = available
        self._response = response

    def complete(self, system, user):
        return self._response


def _unit(qualname, calls, instrumented=False, doc=""):
    return CodeUnit(file="app.py", qualname=qualname, kind="function",
                    signature="(self)", docstring=doc, calls=calls,
                    start_line=1, end_line=9, has_instrumentation=instrumented)


_BLIND = _unit("create_order", ["redis.get", "httpx.post"], doc="create an order")


def test_knowledge_for():
    k = knowledge_for(["cache", "http", "nope"])
    assert "cache" in k and "http" in k and "nope" not in k
    assert k["cache"]  # 有推荐观测


def test_judge_parses_instrument_verdict():
    resp = ('{"verdict":"instrument","confidence":0.9,'
            '"suggestions":[{"type":"metric","what":"cache hit rate"}],'
            '"evidence":["app.py::peer","knowledge:cache"],"reason":"calls cache w/o log"}')
    v = judge_intent(_BLIND, None, StubLLM(True, resp))
    assert v.verdict == "instrument"
    assert v.confidence == 0.9
    assert v.status == "ok"
    assert "knowledge:cache" in v.evidence
    assert v.suggestions and v.suggestions[0]["type"] == "metric"


def test_judge_low_confidence_marked_uncertain():
    resp = '{"verdict":"instrument","confidence":0.3,"suggestions":[],"evidence":[],"reason":"weak"}'
    v = judge_intent(_BLIND, None, StubLLM(True, resp))
    assert v.status == "uncertain"        # < 0.5 → 存疑


def test_judge_parse_error():
    v = judge_intent(_BLIND, None, StubLLM(True, "sorry, I cannot output JSON"))
    assert v.status == "parse_error"
    assert v.verdict == "skip"


def test_judge_degrades_without_llm():
    v = judge_intent(_BLIND, None, StubLLM(available=False))
    assert v.status == "llm_unavailable"
    assert v.verdict == "instrument"                 # 有信号
    assert v.suggestions                             # 来自知识库
    assert any(e.startswith("knowledge:") for e in v.evidence)


def test_peers_only_instrumented_and_not_self():
    idx = CodeIndex(embedder=HashEmbedder(dim=128), store=MemoryStore(128))
    peer_ok = _unit("get_cached_user", ["redis.get"], instrumented=True, doc="read user cache")
    peer_no = _unit("compute_total", ["redis.get"], instrumented=False, doc="sum cache")
    idx.index([_BLIND, peer_ok, peer_no])
    got = {p["unit_id"] for p in _peers(_BLIND, idx)}
    assert "app.py::get_cached_user" in got           # 已埋点的相似函数
    assert "app.py::compute_total" not in got         # 未埋点的排除
    assert "app.py::create_order" not in got          # 自己排除
