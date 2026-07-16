"""认知层（Embedder / VectorStore / CodeIndex）测试。

默认用 HashEmbedder + MemoryStore：不联网、不下载模型、不依赖 qdrant，CI 友好。
另有一个 QdrantStore 测试，装了 qdrant-client 才跑。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.model.code_unit import CodeUnit  # noqa: E402
from sentinel.cognition import (  # noqa: E402
    CodeIndex,
    HashEmbedder,
    MemoryStore,
    embedding_text,
    stable_id,
)


def _unit(qualname, doc, calls):
    return CodeUnit(file="app.py", qualname=qualname, kind="function",
                    signature="(self)", docstring=doc, calls=calls,
                    start_line=1, end_line=5)


def test_embedding_text_shape():
    u = _unit("Svc.get_user", "read user from cache", ["redis.get"])
    text = embedding_text(u)
    assert "Svc.get_user" in text
    assert "read user from cache" in text
    assert "redis.get" in text


def test_hash_embedder_deterministic():
    e = HashEmbedder(dim=64)
    a = e.embed_one("redis cache user")
    b = e.embed_one("redis cache user")
    assert a == b               # 相同文本 → 相同向量
    assert len(a) == 64         # 维度正确


def test_memory_store_upsert_and_search():
    store = MemoryStore(dim=3)
    store.add(["a", "b"], [[1, 0, 0], [0, 1, 0]], [{"n": "a"}, {"n": "b"}])
    assert store.count() == 2
    hits = store.search([0.9, 0.1, 0], k=1)
    assert hits[0].payload["n"] == "a"
    # upsert：同 id 覆盖，不新增
    store.add(["a"], [[0, 0, 1]], [{"n": "a2"}])
    assert store.count() == 2


def test_code_index_retrieve_picks_relevant():
    # HashEmbedder 是词面级：查询词与目标块文本重叠越多越近。
    units = [
        _unit("create_order", "create an order and charge payment", ["requests.post", "db.execute"]),
        _unit("get_user", "read user profile from redis cache", ["redis.get"]),
    ]
    idx = CodeIndex(embedder=HashEmbedder(dim=128), store=MemoryStore(128))
    assert idx.index(units) == 2
    hits = idx.retrieve("redis cache user profile", k=2)
    assert hits[0].payload["qualname"] == "get_user"     # 最相关的排第一
    assert hits[0].unit_id == "app.py::get_user"


def test_stable_id_reproducible():
    assert stable_id("app.py::get_user") == stable_id("app.py::get_user")
    assert stable_id("a") != stable_id("b")


def test_qdrant_store_roundtrip():
    qdrant = pytest.importorskip("qdrant_client")  # 没装就跳过
    from sentinel.cognition import QdrantStore
    store = QdrantStore(dim=3)  # 本地内存模式
    store.add([stable_id("x"), stable_id("y")],
              [[1, 0, 0], [0, 1, 0]], [{"n": "x"}, {"n": "y"}])
    assert store.count() == 2
    hits = store.search([0.9, 0.1, 0.0], k=1)
    assert hits[0].payload["n"] == "x"
    assert hits[0].score > 0.9
