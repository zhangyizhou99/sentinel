"""代码单元（CodeUnit）—— 仓库切块的最小单位（对应 DESIGN §3.2）。

一个 CodeUnit = 一个函数/方法，是「一个可观测性决策单位」：
它调了什么依赖（calls）、有没有埋点（has_instrumentation），
决定了它是不是监控盲区。也是第 3 步向量化/检索的最小切块。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List


@dataclass
class CodeUnit:
    file: str                                   # 相对仓库的路径
    qualname: str                               # 限定名，如 OrderService.create
    kind: str                                   # function / method
    signature: str                              # 形如 (self, order) -> Result
    docstring: str = ""
    decorators: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)   # 被调用的点号名，如 redis.get / db.query
    start_line: int = 0
    end_line: int = 0
    has_instrumentation: bool = False           # 是否已埋点（打了 log / metrics / trace）

    @property
    def unit_id(self) -> str:
        """内容无关的稳定标识：文件 + 限定名。"""
        return f"{self.file}::{self.qualname}"

    @property
    def content_hash(self) -> str:
        """基于结构签名的哈希，供后续变更检测用（内容变了才重算）。"""
        raw = f"{self.unit_id}|{self.signature}|{','.join(sorted(self.calls))}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id, "file": self.file, "qualname": self.qualname,
            "kind": self.kind, "signature": self.signature, "docstring": self.docstring,
            "decorators": self.decorators, "calls": self.calls,
            "start_line": self.start_line, "end_line": self.end_line,
            "has_instrumentation": self.has_instrumentation,
            "content_hash": self.content_hash,
        }
