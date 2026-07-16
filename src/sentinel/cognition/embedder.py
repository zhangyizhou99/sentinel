"""向量化（Embedding）—— 把代码单元的语义变成向量（DESIGN §5.1）。

抽象 `Embedder`：上层只认 `embed(texts) -> 向量列表` + `dim`，不关心底层是谁。
- 默认 `FastEmbedEmbedder`：本地 fastembed（ONNX，无 torch，代码不出本机 · air-gapped）。
- 测试 `HashEmbedder`：哈希技巧的确定性假向量，零依赖/不联网/不下载模型。

「块 → 喂给 embedding 的文本」拼法（Q1 决定）：qualname+signature+docstring+calls，
不含整段源码；改动人等元数据不进 embedding，另存 payload（见 index.py）。
"""
from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from typing import List, Optional

from sentinel.model.code_unit import CodeUnit


def embedding_text(unit: CodeUnit) -> str:
    """把一个代码单元拼成「喂给 embedding 的语义摘要」文本。"""
    lines = [f"{unit.kind} {unit.qualname}{unit.signature}"]
    if unit.docstring:
        lines.append(unit.docstring.strip())
    if unit.calls:
        lines.append("calls: " + ", ".join(unit.calls))
    return "\n".join(lines)


class Embedder(ABC):
    """向量化器抽象：把一批文本变成一批等长向量。"""

    dim: int  # 向量维度

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        ...

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]


class FastEmbedEmbedder(Embedder):
    """本地 fastembed 后端（默认）。首次使用会下载小模型，之后缓存离线可用。"""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        self._model = None
        self.dim = 384  # bge-small-en-v1.5 的维度

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding  # 懒加载可选依赖
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, texts: List[str]) -> List[List[float]]:
        model = self._ensure()
        # fastembed 返回 numpy 向量的生成器；转成 list[list[float]]
        return [vec.tolist() for vec in model.embed(texts)]


class HashEmbedder(Embedder):
    """零依赖的确定性假向量（仅供测试/离线兜底）。

    原理（哈希技巧 / hashing trick）：把文本切成 token，对每个 token 哈希到某个维度上
    累加，最后 L2 归一化。它**没有语义理解**，但对「相同文本→相同向量、相似词面→较近」
    足够用来测试检索管线是否连通。绝不用于生产。
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for tok in _tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _tokenize(text: str) -> List[str]:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def default_embedder(prefer_local: bool = True) -> Embedder:
    """选一个可用的 embedder：优先本地 fastembed，没装则退回 HashEmbedder。"""
    if prefer_local:
        try:
            import fastembed  # noqa: F401
            return FastEmbedEmbedder()
        except Exception:  # noqa: BLE001 —— 没装/加载失败就兜底
            pass
    return HashEmbedder()
