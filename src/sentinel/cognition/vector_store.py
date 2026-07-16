"""向量库（VectorStore）—— 存向量 + 最近邻检索（DESIGN §5.2）。

抽象 `VectorStore`：上层只认 `add(...)` / `search(向量, k)`，不关心底层是谁。
- 默认 `QdrantStore`：Qdrant 本地嵌入式模式（进程内，无需 Docker 服务）。
- 测试/兜底 `MemoryStore`：numpy 暴力余弦（不依赖 qdrant，CI 友好）。

关键认知：embedding 决定「准不准」，向量库决定「快不快/多大」——两者正交，
所以 MemoryStore 与 Qdrant 在我们规模下召回一致；Qdrant 价值在规模化（ANN/持久化/过滤）。
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 稳定 ID 命名空间：把字符串 unit_id 映射成 Qdrant 认的 UUID（可复现）。
_NS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def stable_id(key: str) -> str:
    """由字符串键生成稳定 UUID（同一 key 永远同一 id，便于 upsert 去重）。"""
    return str(uuid.uuid5(_NS, key))


@dataclass
class Hit:
    """一次检索命中：id + 相似度分数 + 附带元数据。"""
    id: str
    score: float
    payload: Dict[str, Any]


class VectorStore(ABC):
    dim: int

    @abstractmethod
    def add(self, ids: List[str], vectors: List[List[float]],
            payloads: List[Dict[str, Any]]) -> None:
        ...

    @abstractmethod
    def search(self, vector: List[float], k: int = 5) -> List[Hit]:
        ...

    @abstractmethod
    def count(self) -> int:
        ...


class MemoryStore(VectorStore):
    """numpy 暴力余弦检索（测试/兜底）。几千向量足够快，O(N) 查询。"""

    def __init__(self, dim: int):
        import numpy as np  # 已装
        self._np = np
        self.dim = dim
        self._ids: List[str] = []
        self._payloads: List[Dict[str, Any]] = []
        self._mat = np.zeros((0, dim), dtype="float32")

    def add(self, ids, vectors, payloads):
        np = self._np
        arr = np.asarray(vectors, dtype="float32")
        # L2 归一化 → 点积即余弦相似度
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norms, 1e-8, None)
        # upsert 语义：同 id 覆盖
        for i, _id in enumerate(ids):
            if _id in self._ids:
                j = self._ids.index(_id)
                self._mat[j] = arr[i]
                self._payloads[j] = payloads[i]
            else:
                self._ids.append(_id)
                self._payloads.append(payloads[i])
                self._mat = np.vstack([self._mat, arr[i:i + 1]])

    def search(self, vector, k=5):
        np = self._np
        if len(self._ids) == 0:
            return []
        q = np.asarray(vector, dtype="float32")
        q = q / (np.linalg.norm(q) or 1.0)
        sims = self._mat @ q
        order = np.argsort(-sims)[:k]
        return [Hit(self._ids[i], float(sims[i]), self._payloads[i]) for i in order]

    def count(self):
        return len(self._ids)


class QdrantStore(VectorStore):
    """Qdrant 本地嵌入式模式（默认）。path=None 走内存，传 path 则文件持久化。"""

    def __init__(self, dim: int, collection: str = "code_units", path: Optional[str] = None):
        from qdrant_client import QdrantClient, models  # 懒加载可选依赖
        self._models = models
        self.dim = dim
        self.collection = collection
        self._client = QdrantClient(path=path) if path else QdrantClient(location=":memory:")
        if not self._client.collection_exists(collection):
            self._client.create_collection(
                collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )

    def add(self, ids, vectors, payloads):
        models = self._models
        points = [
            models.PointStruct(id=_id, vector=vec, payload=pl)
            for _id, vec, pl in zip(ids, vectors, payloads)
        ]
        self._client.upsert(self.collection, points=points)

    def search(self, vector, k=5):
        res = self._client.query_points(self.collection, query=vector, limit=k, with_payload=True)
        return [Hit(str(p.id), float(p.score), p.payload or {}) for p in res.points]

    def count(self):
        return self._client.count(self.collection).count


def default_store(dim: int, prefer_qdrant: bool = True, path: Optional[str] = None) -> VectorStore:
    """优先 Qdrant 本地模式，没装则退回 MemoryStore。"""
    if prefer_qdrant:
        try:
            return QdrantStore(dim, path=path)
        except Exception:  # noqa: BLE001
            pass
    return MemoryStore(dim)
