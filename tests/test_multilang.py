"""多语言自扩展扫描器测试（catalog / query_provider / TreeSitterScanner / 工具）。

注意：解析器注册表是进程内全局的。本文件只注册 typescript/tsx（.ts/.tsx），
不碰 .js，避免污染 test_scanners 里 "app.js 无解析器" 的断言。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.scanners import catalog  # noqa: E402
from sentinel.scanners import query_provider as qp  # noqa: E402
from sentinel.scanners.treesitter_scanner import (  # noqa: E402
    build_scanner,
    register_language,
    install_language_support,
)
from sentinel.engines.scan import scan_repo, signals_of  # noqa: E402
from sentinel.engines.agent_tools import build_check_language_tool  # noqa: E402


def _write(d: str, name: str, text: str) -> None:
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


def test_catalog_mapping():
    assert catalog.language_for_ext(".ts") == "typescript"
    assert catalog.language_for_ext(".JS") == "javascript"      # 大小写无关
    assert catalog.language_for_ext(".go") == "go"
    assert catalog.language_for_ext(".md") == ""                # 不认识
    assert catalog.extensions_for_language("typescript") == [".ts"]


def test_query_provider_builtin_and_missing():
    q = qp.get_queries("typescript")
    assert q and "functions" in q and "calls" in q
    ok, _ = qp.compile_ok("typescript", q["functions"])         # 内置能编译
    assert ok
    assert qp.get_queries("klingon", llm=None) is None          # 无内置/缓存/LLM → None


def test_analyze_repo_detects_gap():
    d = tempfile.mkdtemp()
    _write(d, "a.py", "def f():\n    import redis\n")
    _write(d, "b.go", "package main\nfunc F() {}\n")
    _write(d, "c.md", "# doc\n")
    gap = catalog.analyze_repo(d)
    assert "python" in gap.supported                            # .py 已注册
    assert "go" in gap.extendable                               # .go 认识但没解析器
    assert ".md" in gap.unknown                                 # 不认识
    assert gap.needs_extension is True


def test_treesitter_scans_typescript_blind_spots():
    register_language("typescript")
    d = tempfile.mkdtemp()
    _write(d, "svc.ts",
           "export function getUser(id: string) { return redis.get(id); }\n"
           "export function saveOrder(o: any) { logger.info('x'); return db.execute(o); }\n"
           "const ping = () => fetch('/h');\n")
    res = scan_repo(d)
    names = {u.qualname for u in res.units}
    assert {"getUser", "saveOrder", "ping"} <= names           # 含箭头函数
    blind = {u.qualname for u in res.blind_spots}
    assert "getUser" in blind and "ping" in blind              # 无埋点 → 盲区（fetch 被 JS 信号包识别）
    assert "saveOrder" not in blind                            # 有 logger → 排除
    getuser = next(u for u in res.units if u.qualname == "getUser")
    assert signals_of(getuser) == ["cache"]


def test_signal_words_are_language_scoped():
    """L1 信号按语言分包：前端 fetch/axios 被识别，Python 专属库名不跨语言误配。"""
    from sentinel.scanners.signals import signals_in_calls
    # 前端真实网络库被识别
    assert signals_in_calls(["axios.get"], "typescript") == ["http"]
    assert signals_in_calls(["fetch"], "tsx") == ["http"]
    # Python 专属库名（httpx）不在 JS 包里，不跨语言误配
    assert signals_in_calls(["httpx.get"], "typescript") == []
    # 但在 Python 包里能识别
    assert signals_in_calls(["httpx.get"], "python") == ["http"]
    # 未知语言用并集兑底（宽松召回）
    assert "http" in signals_in_calls(["fetch"], "")


def test_install_language_support_ok():
    res = install_language_support("tsx")                       # 环境已装 language-pack
    assert res["ok"] is True
    assert res["extensions"] == [".tsx"]


def test_build_scanner_unknown_language_returns_none():
    assert build_scanner("klingon") is None                     # 拿不到查询


def test_check_language_tool_reports_gap():
    tool = build_check_language_tool(broker=None)
    d = tempfile.mkdtemp()
    _write(d, "a.py", "def f(): pass\n")
    _write(d, "b.go", "package main\n")
    out = tool.func(d)
    assert "python" in out["supported"]
    assert "go" in out["extendable"]
    assert out["needs_extension"] is True
