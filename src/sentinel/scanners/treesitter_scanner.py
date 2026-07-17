"""通用 tree-sitter 解析器 —— 一套代码适配任意语言。

给定「语言名 + 该语言的 functions/calls 查询」，就能把源码切成函数级 CodeUnit：
用 @fn 定位函数节点与起止行、@name 取函数名、在函数子树上跑 calls 查询取调用点。
是否已埋点 / 命中什么信号，全复用跨语言共享判据（instrumentation + scan.OBS_SIGNALS），
所以新增一门语言 = 注册一个本类实例，下游一行不改。

解析是确定性的（tree-sitter）；LLM 只在「为新语言写查询」时出现一次（见 query_provider），
且被编译校验挡住幻觉。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sentinel.model.code_unit import CodeUnit
from sentinel.scanners.base import LanguageScanner, register
from sentinel.scanners.instrumentation import has_instrumentation
from sentinel.scanners.catalog import extensions_for_language
from sentinel.scanners.query_provider import get_queries


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _signature(fn_node, src: bytes) -> str:
    """尽量取形参列表文本；取不到就回退 '()'。"""
    for child in fn_node.children:
        if child.type.endswith("parameters") or child.type == "formal_parameters":
            return _text(child, src)
    return "()"


class TreeSitterScanner(LanguageScanner):
    """由语言名 + 查询驱动的通用解析器。"""

    def __init__(self, language: str, extensions: List[str], queries: Dict[str, str]):
        self.language = language
        self.EXTENSIONS = tuple(extensions)
        self._queries = queries
        self._parser = None
        self._fn_query = None
        self._call_query = None

    def _ensure_loaded(self) -> bool:
        if self._parser is not None:
            return True
        try:
            from tree_sitter_language_pack import get_parser, get_language
            self._parser = get_parser(self.language)  # type: ignore[arg-type]
            lang = get_language(self.language)         # type: ignore[arg-type]
            self._fn_query = lang.query(self._queries["functions"])
            self._call_query = lang.query(self._queries["calls"])
            return True
        except Exception:  # noqa: BLE001  解析器/查询不可用 → 容错跳过
            self._parser = None
            return False

    def scan_file(self, path: str, rel_path: str) -> List[CodeUnit]:
        if not self._ensure_loaded():
            return []
        try:
            with open(path, "rb") as f:
                src = f.read()
        except OSError:
            return []
        try:
            tree = self._parser.parse(src)
        except Exception:  # noqa: BLE001
            return []

        units: List[CodeUnit] = []
        for _pattern_idx, caps in self._fn_query.matches(tree.root_node):
            fn_nodes = caps.get("fn") or []
            name_nodes = caps.get("name") or []
            if not fn_nodes:
                continue
            fn_node = fn_nodes[0]
            qualname = _text(name_nodes[0], src) if name_nodes else "<anonymous>"

            calls = self._calls_in(fn_node, src)
            blob = " ".join(calls) + " " + _text(fn_node, src)
            units.append(CodeUnit(
                file=rel_path,
                qualname=qualname,
                kind="method" if "method" in fn_node.type else "function",
                signature=_signature(fn_node, src),
                docstring="",
                decorators=[],
                calls=sorted(set(calls)),
                start_line=fn_node.start_point[0] + 1,
                end_line=fn_node.end_point[0] + 1,
                has_instrumentation=has_instrumentation(blob, self.language),
                language=self.language,
            ))
        return units

    def _calls_in(self, fn_node, src: bytes) -> List[str]:
        caps = self._call_query.captures(fn_node)
        callees = caps.get("callee", []) if isinstance(caps, dict) else []
        return [_text(n, src) for n in callees]


def build_scanner(language: str, llm=None) -> Optional[TreeSitterScanner]:
    """为某语言构造解析器：拿查询（内置/缓存/LLM）→ 建实例。拿不到查询返回 None。"""
    queries = get_queries(language, llm=llm)
    if not queries:
        return None
    exts = extensions_for_language(language)
    if not exts:
        return None
    return TreeSitterScanner(language, exts, queries)


def register_language(language: str, llm=None) -> Optional[TreeSitterScanner]:
    """构造并注册某语言解析器（注册后 get_scanner_for 即可命中）。成功返回实例。"""
    scanner = build_scanner(language, llm=llm)
    if scanner is None:
        return None
    register(scanner)
    return scanner


def language_pack_available() -> bool:
    """tree-sitter-language-pack 是否可用（决定要不要先装）。"""
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except ImportError:
        return False


# 有内置查询（编译验证过、不需 LLM）的语言：启动时自动注册，免得重启后要重新「装」。
_BUILTIN_LANGUAGES = ("javascript", "typescript", "tsx")


def register_builtin_languages() -> list:
    """启动时自动注册内置查询的语言（js/ts/tsx）。

    修复「重启后 ts/tsx 又不认识」：注册是进程内内存态，重启会丢；而这些语言的查询是内置的、
    确定性的，language-pack 已装时应自动恢复注册，不该让用户每次重装。language-pack 不在则跳过。
    """
    if not language_pack_available():
        return []
    registered = []
    for lang in _BUILTIN_LANGUAGES:
        try:
            if register_language(lang) is not None:
                registered.append(lang)
        except Exception:  # noqa: BLE001  某语言注册失败不影响其它
            pass
    return registered



def install_language_support(language: str, llm=None) -> Dict[str, object]:
    """补齐某语言的解析能力（**破坏性**：可能 pip 安装，须人审后调用）。

    步骤：确保 tree-sitter-language-pack 在场（不在则 pip 安装）→ 拿查询（内置/缓存/LLM）
    → 注册解析器。返回结构化状态，供 Web/CLI 展示。
    """
    if not language_pack_available():
        import subprocess
        import sys
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--user", "--quiet",
                 "tree-sitter-language-pack"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "language": language,
                    "reason": f"安装 tree-sitter-language-pack 失败 | pip install failed: {e}"}
        if not language_pack_available():
            return {"ok": False, "language": language,
                    "reason": "安装后仍无法导入 tree-sitter-language-pack | still unavailable"}

    scanner = register_language(language, llm=llm)
    if scanner is None:
        return {"ok": False, "language": language,
                "reason": "没能获得该语言的 tree-sitter 查询（无内置/缓存，LLM 也未产出可编译查询）"
                          " | could not obtain a compilable query for this language"}
    return {"ok": True, "language": language, "extensions": list(scanner.EXTENSIONS)}
