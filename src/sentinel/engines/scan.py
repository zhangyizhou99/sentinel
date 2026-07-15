"""代码扫描器（第 2 步）—— 语言无关的编排层。

流程：遍历仓库 → 按扩展名选对应语言解析器（scanners/）→ 得到 CodeUnit
     → 依据「调了关键依赖却没埋点」判定监控盲区（Drift）。

解析（源码→CodeUnit）已下沉到可插拔的 scanners 后端；本文件只做：
遍历/跳过目录、编排、盲区判定。多语言 = 再注册一个后端，本文件不动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

from sentinel.model.code_unit import CodeUnit
from sentinel.scanners import get_scanner_for

# 跳过的目录（生成物 / 依赖 / 虚拟环境等）。
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
              "dist", "build", ".mypy_cache", ".pytest_cache", "site-packages"}

# 「调用点号名里含这些子串」→ 对应可观测性信号（该函数是监控候选）。
OBS_SIGNALS: Dict[str, str] = {
    "redis": "cache", "memcache": "cache",
    "execute": "db", "query": "db", "cursor": "db", "session": "db",
    "sqlalchemy": "db", "psycopg": "db", "pymysql": "db", "sqlite": "db",
    "requests": "http", "httpx": "http", "urllib": "http", "aiohttp": "http", "urlopen": "http",
    "boto3": "cloud", "kafka": "queue", "pika": "queue", "celery": "queue",
    "socket": "network",
}


@dataclass
class ScanResult:
    """一次扫描的结果。"""
    repo: str
    units: List[CodeUnit] = field(default_factory=list)

    @property
    def blind_spots(self) -> List[CodeUnit]:
        """监控盲区：调了关键依赖、却没埋点的函数（Drift）。"""
        return [u for u in self.units if signals_of(u) and not u.has_instrumentation]

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "total_units": len(self.units),
            "blind_spots": [u.to_dict() for u in self.blind_spots],
        }


def signals_of(unit: CodeUnit) -> List[str]:
    """这个函数命中的可观测性信号（去重）。"""
    found = set()
    for call in unit.calls:
        low = call.lower()
        for key, sig in OBS_SIGNALS.items():
            if key in low:
                found.add(sig)
    return sorted(found)


def scan_file(path: str, rel_path: str) -> List[CodeUnit]:
    """解析单个文件；按扩展名选解析器，没有对应解析器则返回空。"""
    scanner = get_scanner_for(rel_path)
    if scanner is None:
        return []
    return scanner.scan_file(path, rel_path)


def scan_repo(repo_path: str) -> ScanResult:
    """扫描整个仓库（或单个文件）。只处理有对应语言解析器的文件。"""
    result = ScanResult(repo=repo_path)
    if os.path.isfile(repo_path):
        result.units.extend(scan_file(repo_path, os.path.basename(repo_path)))
        return result
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]  # 就地裁剪要跳过的目录
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_path)
            if get_scanner_for(rel) is None:  # 无对应解析器的文件直接跳过
                continue
            result.units.extend(scan_file(full, rel))
    return result
