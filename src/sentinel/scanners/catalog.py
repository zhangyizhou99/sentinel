"""语言目录 —— 扩展名 → tree-sitter 语言名 的确定性映射，以及仓库「能力缺口」检测。

职责：
1. 把文件扩展名映射到 tree-sitter-language-pack 的语言名（纯查表，不含 LLM）。
2. 扫一个仓库，统计各扩展名文件数，并按「当前能不能解析」分三类：
   - supported ：已注册解析器（get_scanner_for 命中），可直接扫。
   - extendable：目录里认识这个扩展、tree-sitter 也能解析，但还没注册解析器
                 → 这些就是「自我补齐」的候选（人审后装/注册）。
   - unknown   ：目录里都不认识的扩展，跳过。
"""
from __future__ import annotations

import os
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from sentinel.scanners.base import supported_extensions

# 扩展名 → tree-sitter-language-pack 语言名。故意只列主流、可观测性相关度高的。
EXT_TO_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".scala": "scala",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
}

_DYNAMIC_CATALOG_PATH = os.path.expanduser("~/.cache/sentinel/language_extensions.json")


def _dynamic_mappings() -> Dict[str, str]:
    try:
        with open(_DYNAMIC_CATALOG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(ext).lower(): str(language).lower() for ext, language in data.items()
                if str(ext).startswith(".") and str(language)}
    except (OSError, ValueError, TypeError):
        return {}


def register_language_extensions(language: str, extensions: List[str]) -> Dict[str, str]:
    """持久化用户确认的扩展名 -> tree-sitter 语言映射。"""
    language = (language or "").strip().lower()
    normalized = [ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                  for ext in extensions if str(ext).strip()]
    if not language or not normalized:
        raise ValueError("需要语言名和至少一个扩展名")
    mapping = _dynamic_mappings()
    mapping.update({ext: language for ext in normalized})
    os.makedirs(os.path.dirname(_DYNAMIC_CATALOG_PATH), exist_ok=True)
    with open(_DYNAMIC_CATALOG_PATH, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return {ext: language for ext in normalized}

# 与 scan.py 保持一致的跳过目录。
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
              "dist", "build", ".mypy_cache", ".pytest_cache", "site-packages",
              ".next", "coverage", "out", "target"}


def language_for_ext(ext: str) -> str:
    """扩展名 → 语言名（未知返回空串）。"""
    normalized = ext.lower()
    return EXT_TO_LANGUAGE.get(normalized, _dynamic_mappings().get(normalized, ""))


@dataclass
class LanguageGap:
    """一次仓库能力扫描的结果。"""
    repo: str
    file_counts: Dict[str, int] = field(default_factory=dict)  # 扩展名 → 文件数
    supported: Dict[str, int] = field(default_factory=dict)    # 语言 → 文件数（已可扫）
    extendable: Dict[str, int] = field(default_factory=dict)   # 语言 → 文件数（可补齐）
    unknown: Dict[str, int] = field(default_factory=dict)      # 扩展名 → 文件数（不认识）

    @property
    def needs_extension(self) -> bool:
        return bool(self.extendable)

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "supported": self.supported,
            "extendable": self.extendable,
            "unknown": self.unknown,
        }


def analyze_repo(root: str) -> LanguageGap:
    """遍历仓库，统计各扩展名，并分成 supported / extendable / unknown 三类。"""
    counts: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext:
                counts[ext] += 1

    gap = LanguageGap(repo=root, file_counts=dict(counts))
    registered = supported_extensions()
    for ext, n in counts.items():
        lang = language_for_ext(ext)
        if not lang:
            continue  # 目录里根本不认识 → 交给下方 unknown
        if ext in registered:
            gap.supported[lang] = gap.supported.get(lang, 0) + n
        else:
            gap.extendable[lang] = gap.extendable.get(lang, 0) + n
    # unknown：出现较多、但不在语言表里的扩展（提示用，不强制）
    for ext, n in counts.items():
        if not language_for_ext(ext):
            gap.unknown[ext] = n
    return gap


def extensions_for_language(language: str) -> List[str]:
    """反查：某语言对应哪些扩展名（注册解析器时用）。"""
    known = {**EXT_TO_LANGUAGE, **_dynamic_mappings()}
    return [ext for ext, lang in known.items() if lang == language]
