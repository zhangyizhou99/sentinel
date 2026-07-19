"""查询提供者 —— 为某语言拿到 tree-sitter 查询（functions / calls）。

三级来源，优先级从高到低：
1. 内置：JS/TS/TSX 等主流语言手写并验证过的查询，稳、无需联网。
2. 缓存：之前由 LLM 现写、且**编译通过**的查询，落盘复用。
3. LLM 现写：遇到没见过的语言，让模型照着约定写查询，再用真语法**编译校验**
   （编译不过就把报错回灌重试），通过才缓存。编译校验是关键护栏：
   语法节点名是模型最容易幻觉的地方，编译一跑，幻觉直接被拒。

约定的捕获名（下游只认这三个）：
  @fn     整个函数/方法节点（用于定位起止行、取子树扫调用）
  @name   函数名标识符
  @callee 一次调用的被调方（identifier 或 member_expression，如 logger.info）
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

# JS/TS/TSX 共用一套（TypeScript 语法是 JS 超集，这些节点名通用）。已在 py3.9 + 0.23 三语法验证。
_JS_LIKE = {
    "functions": (
        "(function_declaration name: (identifier) @name) @fn "
        "(method_definition name: (property_identifier) @name) @fn "
        "(variable_declarator name: (identifier) @name "
        "  value: [(arrow_function) (function_expression)] ) @fn"
    ),
    "calls": "(call_expression function: (_) @callee)",
}

_BUILTIN_QUERIES: Dict[str, Dict[str, str]] = {
    "javascript": _JS_LIKE,
    "typescript": _JS_LIKE,
    "tsx": _JS_LIKE,
}

_CACHE_DIR = os.path.expanduser("~/.cache/sentinel/ts_queries")

# —— 与用户共创的双语提示词（rule 1）。让 LLM 只干「写查询」这一件事，且格式严格。——
_QUERY_SYSTEM = """You author tree-sitter S-expression queries. / 你负责编写 tree-sitter 查询。

Given a LANGUAGE, output two queries that work against that language's official tree-sitter grammar.
给定一门语言，输出两条能在该语言官方 tree-sitter 语法上运行的查询。

Capture names are FIXED — use exactly these / 捕获名是固定的，必须精确使用：
  @fn     the whole function/method node        整个函数/方法节点
  @name   the function's name identifier        函数名标识符
  @callee the callee of a call expression        一次调用的被调方

Rules / 规则：
- "functions" may contain several patterns (declarations, methods, lambdas assigned to a name).
  functions 可含多条模式（函数声明、类方法、赋值给名字的匿名函数）。
- "calls" matches call sites; @callee should capture the callable (identifier or member access).
  calls 匹配调用点；@callee 捕获被调对象（标识符或成员访问）。
- Use ONLY node types that exist in that grammar. Do not invent node names.
  只用该语法里真实存在的节点名，不要臆造。
- Output STRICT JSON, nothing else / 只输出严格 JSON，别的都不要：
  {"functions": "<query>", "calls": "<query>"}

Example for JavaScript / JavaScript 示例：
{"functions": "(function_declaration name: (identifier) @name) @fn (method_definition name: (property_identifier) @name) @fn", "calls": "(call_expression function: (_) @callee)"}
"""


def _cache_path(language: str) -> str:
    return os.path.join(_CACHE_DIR, f"{language}.json")


def _load_cache(language: str) -> Optional[Dict[str, str]]:
    path = _cache_path(language)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "functions" in data and "calls" in data:
            return {"functions": data["functions"], "calls": data["calls"]}
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _save_cache(language: str, queries: Dict[str, str]) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(language), "w", encoding="utf-8") as f:
            json.dump(queries, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def compile_ok(language: str, query_str: str) -> Tuple[bool, str]:
    """把查询在真语法上编译一遍。通过返回 (True, "")，否则 (False, 报错)。"""
    try:
        from tree_sitter_language_pack import get_language
    except ImportError as e:
        return False, f"tree-sitter-language-pack 未安装：{e}"
    try:
        lang = get_language(language)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001  语言名不被支持
        return False, f"未知语言 {language}：{e}"
    try:
        compile_query(lang, query_str)
        return True, ""
    except Exception as e:  # noqa: BLE001  查询语法/节点名错误
        return False, str(e)


def compile_query(language, query_str: str):
    """编译查询，兼容旧版 ``Language.query`` 与新版 ``Query(language, ...)`` API。"""
    query_method = getattr(language, "query", None)
    if callable(query_method):
        return query_method(query_str)
    from tree_sitter import Query
    return Query(language, query_str)


def _verify(language: str, queries: Dict[str, str]) -> Tuple[bool, str]:
    for kind in ("functions", "calls"):
        ok, err = compile_ok(language, queries.get(kind, ""))
        if not ok:
            return False, f"[{kind}] {err}"
    return True, ""


def get_queries(language: str, llm=None, max_retries: int = 2) -> Optional[Dict[str, str]]:
    """拿到某语言的查询：内置 → 缓存 → LLM 现写（编译校验+重试+缓存）。拿不到返回 None。"""
    if language in _BUILTIN_QUERIES:
        builtin = _BUILTIN_QUERIES[language]
        ok, _ = _verify(language, builtin)
        if ok:
            return builtin  # 内置也过一遍编译，语法漂移时不至于静默失效
        # 内置竟然编译不过（语法版本漂移）→ 往下走缓存/LLM 兜底

    cached = _load_cache(language)
    if cached:
        ok, _ = _verify(language, cached)
        if ok:
            return cached  # 命中且仍能编译

    if llm is None or not getattr(llm, "available", False):
        return None  # 无内置、无缓存、无 LLM → 补不了

    feedback = ""
    for _ in range(max_retries + 1):
        user = f"LANGUAGE / 语言: {language}"
        if feedback:
            user += f"\n\nPrevious attempt failed to compile / 上次编译失败：\n{feedback}\nFix it. / 修正它。"
        try:
            raw = llm.complete(_QUERY_SYSTEM, user)
        except Exception:  # noqa: BLE001
            return None
        parsed = _parse(raw)
        if not parsed:
            feedback = "输出不是合法 JSON，或缺少 functions/calls 字段。"
            continue
        ok, err = _verify(language, parsed)
        if ok:
            _save_cache(language, parsed)
            return parsed
        feedback = err  # 把编译报错回灌，让模型据此修
    return None


def _parse(raw: str) -> Optional[Dict[str, str]]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("functions"), str) and isinstance(data.get("calls"), str):
        return {"functions": data["functions"], "calls": data["calls"]}
    return None
