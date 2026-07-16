"""代码索引与检索（DESIGN §5 / 第8章 RAG）。

把 Embedder + VectorStore 串成一个好用的门面：
- `index(units)`：把一批 CodeUnit 向量化并入库（payload 存元数据，供命中后展示/路由）。
- `retrieve(query, k)`：把自然语言问题向量化，召回最相关的 top-K 代码单元。

大仓里 LLM 塞不下所有函数，就靠它先挑出 top-K 再喂给 LLM 判断（两级漏斗的第一级）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sentinel.model.code_unit import CodeUnit
from sentinel.cognition.embedder import Embedder, default_embedder, embedding_text
from sentinel.cognition.vector_store import Hit, VectorStore, default_store, stable_id


def _payload(unit: CodeUnit) -> Dict[str, Any]:
    """入库时随向量一起存的元数据（不进 embedding，用于命中后展示/过滤/路由）。

    改动人(git blame/author)将来在这里加一个字段即可 —— 用于「找对人补埋点」(§6 blame_route)。
    """
    return {
        "unit_id": unit.unit_id,
        "file": unit.file,
        "qualname": unit.qualname,
        "signature": unit.signature,
        "lines": f"{unit.start_line}-{unit.end_line}",
        "calls": unit.calls,
        "has_instrumentation": unit.has_instrumentation,
        "content_hash": unit.content_hash,
    }


@dataclass
class RetrievedUnit:
    """一次检索命中的对外结果：分数 + 元数据。"""
    score: float
    payload: Dict[str, Any]

    @property
    def unit_id(self) -> str:
        return self.payload.get("unit_id", "")


class CodeIndex:
    """代码单元的向量索引 + 检索门面。"""

    def __init__(self, embedder: Optional[Embedder] = None,
                 store: Optional[VectorStore] = None):
        self.embedder = embedder or default_embedder()
        # store 维度必须与 embedder 对齐；未显式传 store 就按 embedder.dim 建默认库。
        self.store = store or default_store(self.embedder.dim)

    def index(self, units: List[CodeUnit]) -> int:
        """向量化并入库；返回入库条数。相同 unit_id 会 upsert 覆盖。"""
        if not units:
            return 0
        texts = [embedding_text(u) for u in units]
        vectors = self.embedder.embed(texts)
        ids = [stable_id(u.unit_id) for u in units]
        payloads = [_payload(u) for u in units]
        self.store.add(ids, vectors, payloads)
        return len(units)

    def retrieve(self, query: str, k: int = 5) -> List[RetrievedUnit]:
        """给一个自然语言问题，召回最相关的 top-K 代码单元。"""
        qvec = self.embedder.embed_one(query)
        hits: List[Hit] = self.store.search(qvec, k=k)
        return [RetrievedUnit(score=h.score, payload=h.payload) for h in hits]

    def count(self) -> int:
        return self.store.count()
