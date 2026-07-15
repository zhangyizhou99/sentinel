"""代码扫描器（第 2 步）—— 纯静态、不用 LLM、air-gapped 可跑。

流程：遍历仓库 .py → ast 解析 → 抽出每个函数/方法为 CodeUnit（切块）
     → 依据「调了关键依赖却没埋点」判定监控盲区（Drift）。

设计要点：
- 用标准库 ast「读代码结构」，不是正则、不是字符串匹配。
- OBS_SIGNALS：把「调用点号名」映射到可观测性信号（RED/USE 的雏形）。
- INSTRUMENTATION_HINTS：识别函数是否已打 log / metrics / trace。
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sentinel.model.code_unit import CodeUnit

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

# 「函数体里出现这些子串」→ 认为已埋点。
INSTRUMENTATION_HINTS = (
    "logger", "logging", "getlogger", ".log(", "log.info", "log.error",
    "metrics", "meter", "counter", "histogram",
    "tracer", "span", "otel", "opentelemetry", "statsd", "prometheus",
)


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


def _dotted_name(node: ast.AST) -> str:
    """把 ast 的属性/名字链拼成点号名，如 redis.client.get。"""
    parts: List[str] = []
    cur: Optional[ast.AST] = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _signature(fn: ast.AST) -> str:
    """从函数定义节点拼出参数签名（够用即可，不追求完美）。"""
    try:
        args = [a.arg for a in fn.args.args]  # type: ignore[attr-defined]
        if fn.args.vararg:  # type: ignore[attr-defined]
            args.append("*" + fn.args.vararg.arg)  # type: ignore[attr-defined]
        if fn.args.kwarg:  # type: ignore[attr-defined]
            args.append("**" + fn.args.kwarg.arg)  # type: ignore[attr-defined]
        return "(" + ", ".join(args) + ")"
    except AttributeError:
        return "()"


def _extract_from_function(fn: ast.AST, rel_path: str, prefix: str,
                          aliases: Dict[str, str]) -> CodeUnit:
    calls: List[str] = []
    body_src_lower_parts: List[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name:
                calls.append(name)
                # 变量别名解析：r.get 且 r = redis.Redis() → 补一条 redis.get，让信号识别得到。
                base = name.split(".", 1)[0]
                if base in aliases and "." in name:
                    calls.append(aliases[base] + "." + name.split(".", 1)[1])
        # 收集函数体里出现的名字，用于判断是否埋点。
        if isinstance(node, ast.Name):
            body_src_lower_parts.append(node.id.lower())
        elif isinstance(node, ast.Attribute):
            body_src_lower_parts.append(_dotted_name(node).lower())

    body_blob = " ".join(body_src_lower_parts)
    has_instr = any(h in body_blob for h in INSTRUMENTATION_HINTS)

    decorators = [_dotted_name(d) if not isinstance(d, ast.Call) else _dotted_name(d.func)
                  for d in getattr(fn, "decorator_list", [])]
    qualname = f"{prefix}{fn.name}"  # type: ignore[attr-defined]
    kind = "method" if prefix else "function"
    return CodeUnit(
        file=rel_path,
        qualname=qualname,
        kind=kind,
        signature=_signature(fn),
        docstring=ast.get_docstring(fn) or "",
        decorators=[d for d in decorators if d],
        calls=sorted(set(calls)),
        start_line=getattr(fn, "lineno", 0),
        end_line=getattr(fn, "end_lineno", getattr(fn, "lineno", 0)),
        has_instrumentation=has_instr,
    )


def _collect_aliases(tree: ast.AST) -> Dict[str, str]:
    """收集「变量 = 某依赖(...)」的别名映射，如 r = redis.Redis() → {'r': 'redis'}。

    只做轻量版：单个 Name 目标 + 赋值右侧点号名命中 OBS_SIGNALS 的 key。
    足够覆盖「客户端赋给变量再调用」这种最常见写法（第一期，未做 self.x 与跨函数）。
    """
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value.func if isinstance(node.value, ast.Call) else node.value
            rhs = _dotted_name(value).lower()
            for key in OBS_SIGNALS:
                if key in rhs:
                    aliases[node.targets[0].id] = key
                    break
    return aliases


def _walk_body(body: List[ast.AST], rel_path: str, prefix: str,
               aliases: Dict[str, str]) -> List[CodeUnit]:
    """递归遍历模块/类体，抽出函数与方法（含类内嵌套）。"""
    units: List[CodeUnit] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(_extract_from_function(node, rel_path, prefix, aliases))
        elif isinstance(node, ast.ClassDef):
            units.extend(_walk_body(node.body, rel_path, prefix=f"{prefix}{node.name}.", aliases=aliases))
    return units


def scan_file(path: str, rel_path: str) -> List[CodeUnit]:
    """解析单个 .py 文件为 CodeUnit 列表；解析失败返回空（容错，不中断整体）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=rel_path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    aliases = _collect_aliases(tree)
    return _walk_body(tree.body, rel_path, prefix="", aliases=aliases)


def scan_repo(repo_path: str) -> ScanResult:
    """扫描整个仓库（或单个 .py 文件）。"""
    result = ScanResult(repo=repo_path)
    if os.path.isfile(repo_path) and repo_path.endswith(".py"):
        result.units.extend(scan_file(repo_path, os.path.basename(repo_path)))
        return result
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]  # 就地裁剪要跳过的目录
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_path)
            result.units.extend(scan_file(full, rel))
    return result
