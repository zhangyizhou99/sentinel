"""Python 语言解析器（用标准库 ast）。

把源码切成函数/方法级 CodeUnit：抽签名/docstring/装饰器/调用点号名，
并判断是否已埋点。含轻量变量别名解析（r = redis.Redis() 后 r.get 也算 redis）。
这些都是 **Python 专属**逻辑，封装在此；换语言只需另写一个 LanguageScanner。
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional

from sentinel.model.code_unit import CodeUnit
from sentinel.scanners.base import LanguageScanner, register
from sentinel.scanners.instrumentation import INSTRUMENTATION_HINTS  # 埋点判据现为跨语言共享

# 用于别名解析：右侧点号名含这些子串就认为是可观测性依赖（与 OBS_SIGNALS 的 key 一致即可）。
_DEP_KEYS = (
    "redis", "memcache", "execute", "query", "cursor", "session",
    "sqlalchemy", "psycopg", "pymysql", "sqlite",
    "requests", "httpx", "urllib", "aiohttp", "urlopen",
    "boto3", "kafka", "pika", "celery", "socket",
)


def _dotted_name(node: ast.AST) -> str:
    parts: List[str] = []
    cur: Optional[ast.AST] = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _signature(fn: ast.AST) -> str:
    try:
        args = [a.arg for a in fn.args.args]  # type: ignore[attr-defined]
        if fn.args.vararg:  # type: ignore[attr-defined]
            args.append("*" + fn.args.vararg.arg)  # type: ignore[attr-defined]
        if fn.args.kwarg:  # type: ignore[attr-defined]
            args.append("**" + fn.args.kwarg.arg)  # type: ignore[attr-defined]
        return "(" + ", ".join(args) + ")"
    except AttributeError:
        return "()"


def _collect_aliases(tree: ast.AST) -> Dict[str, str]:
    """收集「变量 = 某依赖(...)」的别名，如 r = redis.Redis() → {'r': 'redis'}。"""
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value.func if isinstance(node.value, ast.Call) else node.value
            rhs = _dotted_name(value).lower()
            for key in _DEP_KEYS:
                if key in rhs:
                    aliases[node.targets[0].id] = key
                    break
    return aliases


def _extract_from_function(fn: ast.AST, prefix: str, aliases: Dict[str, str]) -> CodeUnit:
    calls: List[str] = []
    body_parts: List[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name:
                calls.append(name)
                base = name.split(".", 1)[0]
                if base in aliases and "." in name:  # 别名补一条，让信号识别得到
                    calls.append(aliases[base] + "." + name.split(".", 1)[1])
        if isinstance(node, ast.Name):
            body_parts.append(node.id.lower())
        elif isinstance(node, ast.Attribute):
            body_parts.append(_dotted_name(node).lower())

    body_blob = " ".join(body_parts)
    has_instr = any(h in body_blob for h in INSTRUMENTATION_HINTS)
    decorators = [_dotted_name(d) if not isinstance(d, ast.Call) else _dotted_name(d.func)
                  for d in getattr(fn, "decorator_list", [])]
    return CodeUnit(
        file="",  # 由 scan_file 填充
        qualname=f"{prefix}{fn.name}",  # type: ignore[attr-defined]
        kind="method" if prefix else "function",
        signature=_signature(fn),
        docstring=ast.get_docstring(fn) or "",
        decorators=[d for d in decorators if d],
        calls=sorted(set(calls)),
        start_line=getattr(fn, "lineno", 0),
        end_line=getattr(fn, "end_lineno", getattr(fn, "lineno", 0)),
        has_instrumentation=has_instr,
    )


def _walk_body(body: List[ast.AST], prefix: str, aliases: Dict[str, str]) -> List[CodeUnit]:
    units: List[CodeUnit] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(_extract_from_function(node, prefix, aliases))
        elif isinstance(node, ast.ClassDef):
            units.extend(_walk_body(node.body, prefix=f"{prefix}{node.name}.", aliases=aliases))
    return units


class PythonScanner(LanguageScanner):
    EXTENSIONS = (".py",)

    def scan_file(self, path: str, rel_path: str) -> List[CodeUnit]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel_path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            return []
        aliases = _collect_aliases(tree)
        units = _walk_body(tree.body, prefix="", aliases=aliases)
        for u in units:
            u.file = rel_path
        return units


register(PythonScanner())
