"""函数级埋点插入器（Restore 的改写核心）—— 见 DESIGN §8.3。

与 legacy `editor.py`（app 级 FastAPI 中间件）不同：这里**对准某个盲区函数**，在其
函数体首行按缩进插入一行埋点（+ 顶部按需补 import），照项目约定的风格补。

改写手法沿用 legacy 精华：**AST 定位 + 行级插入**（不用 ast.unparse，避免重排格式/丢注释）、
从高行号往低行号插保行号、幂等（已插过就跳过）、AST 安全网（改后必须仍能 parse，否则返回 None）。
"""
from __future__ import annotations

import ast
from typing import Optional


def _is_docstring(node: ast.AST) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _find_func(tree: ast.Module, qualname: str):
    """按限定名（`func` 或 `Class.method`）找函数节点。"""
    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if prefix + node.name == qualname:
                    return node
            if isinstance(node, ast.ClassDef):
                found = walk(node.body, prefix + node.name + ".")
                if found is not None:
                    return found
        return None
    return walk(tree.body, "")


def _last_import_end(tree: ast.Module) -> int:
    """最后一个顶层 import 之后的行号（1-based；无则 0）。"""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno or node.lineno
    return last


def insert_instrumentation(source: str, qualname: str, snippet: str,
                           import_stmt: Optional[str] = None) -> Optional[str]:
    """在 `qualname` 函数体首行插入 `snippet`（按缩进），可选在顶部补 `import_stmt`。

    返回改后完整源码；无法安全改写（找不到函数 / 改后不能 parse）返回 None。幂等。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    func = _find_func(tree, qualname)
    if func is None or not func.body:
        return None

    src_lines = source.splitlines()
    fn_start = func.lineno
    fn_end = func.end_lineno or func.lineno
    fn_text = "\n".join(src_lines[fn_start - 1:fn_end])
    if snippet.strip() and snippet.strip() in fn_text:
        return None  # 幂等：该函数已含此埋点

    # 定位插入点：函数体首个「真实语句」之前；若首句是 docstring 则插到其后。
    first = func.body[0]
    if _is_docstring(first):
        if len(func.body) > 1:
            anchor = func.body[1]
            insert_idx = anchor.lineno - 1          # 0-based：插在该语句前
            indent = anchor.col_offset
        else:
            insert_idx = first.end_lineno or first.lineno  # docstring 之后
            indent = first.col_offset
    else:
        insert_idx = first.lineno - 1
        indent = first.col_offset

    # import 插到最后一个顶层 import 之后（且尚未存在时）。import 在函数之上，
    # 其行号不受「函数体插入」影响，故先在高行号插 snippet、再在低行号插 import。
    need_import = bool(import_stmt) and (import_stmt not in source)
    imp_idx = _last_import_end(tree) if need_import else None

    lines = list(src_lines)
    lines.insert(insert_idx, " " * indent + snippet)   # 高行号，先插
    if imp_idx is not None:
        lines.insert(imp_idx, import_stmt)             # 低行号，后插

    new_source = "\n".join(lines) + ("\n" if source.endswith("\n") else "")
    try:
        ast.parse(new_source)                          # 安全网
    except SyntaxError:
        return None
    return new_source


def insert_js_import(source: str, import_stmt: str) -> str:
    """在首个现有 import 前加入一条 JS/TS import；已存在时保持不变。"""
    if not import_stmt or import_stmt in source:
        return source
    lines = source.splitlines()
    import_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("import ")),
        0,
    )
    lines.insert(import_index, import_stmt)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def insert_js_instrumentation(source: str, start_line: int, end_line: int,
                              snippet: str, import_stmt: Optional[str] = None) -> Optional[str]:
    """在已被 tree-sitter 定位的 JS/TS 函数体首行插入一条埋点。

    不重排代码，也不引入日志库。只接受函数范围内独占一行的开括号，避免对单行函数、
    复杂表达式函数或无法可靠判定的源码做猜测性改写。
    """
    lines = source.splitlines()
    lower = max(0, start_line - 1)
    upper = min(len(lines), max(lower, end_line))
    if lower >= upper:
        return None
    function_text = "\n".join(lines[lower:upper])
    if "sentinel: observability" in function_text:
        return None

    opening_index = None
    for index in range(lower, upper):
        before, marker, after = lines[index].partition("{")
        if not marker:
            continue
        # 仅处理 `{` 后没有同一行语句的函数体，保证插入点仍在函数内部。
        tail = after.strip()
        if tail and not tail.startswith("//") and not tail.startswith("/*"):
            continue
        opening_index = index
        break
    if opening_index is None:
        return None

    indent = len(lines[opening_index]) - len(lines[opening_index].lstrip()) + 2
    for candidate in lines[opening_index + 1:upper]:
        if candidate.strip():
            indent = len(candidate) - len(candidate.lstrip())
            break
    lines.insert(opening_index + 1, " " * indent + "// sentinel: observability")
    lines.insert(opening_index + 2, " " * indent + snippet)

    updated = "\n".join(lines) + ("\n" if source.endswith("\n") else "")
    return insert_js_import(updated, import_stmt or "")
