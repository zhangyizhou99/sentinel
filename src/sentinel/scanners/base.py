"""语言解析器的可插拔接口（多语言就绪）。

每种语言一个 LanguageScanner：吃源码 → 产出统一的 CodeUnit。
下游（盲区检测 / RAG / Agent）全语言无关，只认 CodeUnit。

- Python 用标准库 ast（精确、零依赖）。
- 未来 JS/Go 等用 tree-sitter；冷门语言可加 LLM fallback。
  ——都只是「再注册一个后端」，不动下游。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Dict, List

from sentinel.model.code_unit import CodeUnit


class LanguageScanner(ABC):
    """一种语言的解析器。"""

    EXTENSIONS: tuple = ()  # 负责的文件扩展名，如 (".py",)

    @abstractmethod
    def scan_file(self, path: str, rel_path: str) -> List[CodeUnit]:
        """解析单个文件为 CodeUnit 列表；失败应返回空（容错，不中断整体）。"""
        raise NotImplementedError


# 扩展名 → 解析器 的注册表。
_REGISTRY: Dict[str, LanguageScanner] = {}


def register(scanner: LanguageScanner) -> None:
    for ext in scanner.EXTENSIONS:
        _REGISTRY[ext.lower()] = scanner


def get_scanner_for(rel_path: str):
    """按文件扩展名选解析器（确定性、免费、无需 LLM）。没有则返回 None。"""
    ext = os.path.splitext(rel_path)[1].lower()
    return _REGISTRY.get(ext)


def supported_extensions() -> set:
    return set(_REGISTRY)
