"""scan / find_repo 工具封装测试（Tool ←→ scan/权限门 的桥）。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.agent_tools import build_find_repo_tool, build_scan_tool  # noqa: E402
from sentinel.permissions import PermissionBroker  # noqa: E402

_TESTS = Path(__file__).resolve().parent
_FIXTURES = _TESTS / "fixtures"


def test_scan_tool_finds_blind_spot():
    tool = build_scan_tool()  # 无 broker = 不设权限门（CLI 场景）
    report = tool.func(str(_FIXTURES / "sample_app.py"))
    assert report["blind_spot_count"] >= 1
    funcs = [b["function"] for b in report["blind_spots"]]
    assert any("create_order" in f for f in funcs)
    # 命中的盲区应带有风险信号。
    spot = next(b for b in report["blind_spots"] if "create_order" in b["function"])
    assert spot["signals"]


def test_scan_tool_missing_path_raises():
    tool = build_scan_tool()
    with pytest.raises(FileNotFoundError):
        tool.func("/no/such/path/xyz")


def test_scan_tool_reports_language_gap(tmp_path):
    """混合仓库：.py 能扫，.go 没装解析器 → report 要显式带 language_gap，不能静默吞掉。

    用 .go（全程序里没有任何测试会注册它）而非 .ts，避免与 test_multilang 的全局
    解析器注册（进程内共享 _REGISTRY）产生测试顺序耦合。
    """
    (tmp_path / "a.py").write_text("def f():\n    import redis\n    redis.Redis().get('k')\n")
    (tmp_path / "b.go").write_text("package main\nfunc g() {}\n")
    tool = build_scan_tool()
    report = tool.func(str(tmp_path))
    assert report.get("language_gap") == {"go": 1}
    with pytest.raises(ValueError):
        tool.func("")


def test_scan_tool_permission_gate():
    broker = PermissionBroker(str(_TESTS))
    tool = build_scan_tool(broker)
    target = str(_FIXTURES / "sample_app.py")
    # 未授权 → 返回 permission_required，而非直接读取。
    res = tool.func(target)
    assert res.get("permission_required")
    assert "blind_spots" not in res
    # 授权后 → 正常扫描出盲区。
    broker.grant(str(_FIXTURES))
    res2 = tool.func(target)
    assert res2["blind_spot_count"] >= 1


def test_scan_tool_out_of_scope_denied():
    broker = PermissionBroker(str(_FIXTURES))
    tool = build_scan_tool(broker)
    # tests 的父目录存在、且在 fixtures 范围外 → denied。
    res = tool.func(str(_TESTS.parent))
    assert res.get("denied")


def test_find_repo_locates_directory():
    broker = PermissionBroker(str(_TESTS))
    tool = build_find_repo_tool(broker)
    res = tool.func("fixtures")
    assert any(m.endswith("fixtures") for m in res["matches"])
    assert res["root"] == str(_TESTS)


def test_find_repo_exact_match_is_unique():
    # 目录名精确等于关键词时，只返回它一个（不被同名子串项淹没）。
    broker = PermissionBroker(str(_TESTS))
    tool = build_find_repo_tool(broker)
    res = tool.func("fixtures")
    assert res["matches"] == [str(_FIXTURES)]


def test_find_repo_surfaces_children_for_semantic_pick(tmp_path):
    """匹配目录若有子目录（如 backend/frontend），要如实列出，供 LLM 自行做语义判断，
    而不是让 find_repo/代码去猜"前端"该等于哪个目录名——这是设计原则：
    结构性事实（有哪些真实子目录）用代码列，语义选择交给 LLM。
    """
    proj = tmp_path / "haulhero"
    (proj / "backend").mkdir(parents=True)
    (proj / "frontend").mkdir()
    (proj / "node_modules").mkdir()          # 生成物目录应被过滤，不出现在 children 里
    broker = PermissionBroker(str(tmp_path))
    tool = build_find_repo_tool(broker)
    res = tool.func("haulhero")
    assert res["matches"] == [str(proj)]
    assert res["children"][str(proj)] == ["backend", "frontend"]  # node_modules 被过滤


def test_find_repo_no_children_key_when_leaf_directory():
    """没有子目录的匹配项不应出现在 children 里（保持返回精简）。"""
    broker = PermissionBroker(str(_TESTS))
    tool = build_find_repo_tool(broker)
    res = tool.func("fixtures")
    # fixtures 目录若无子目录，则不会作为 key 出现
    if not any(os.path.isdir(os.path.join(str(_FIXTURES), d)) for d in os.listdir(_FIXTURES)):
        assert str(_FIXTURES) not in res["children"]


def test_find_repo_empty_result_explains_workspace_scope(tmp_path):
    tool = build_find_repo_tool(PermissionBroker(str(tmp_path)))
    res = tool.func("haulhero")
    assert res["matches"] == []
    assert "scope_hint" in res
    assert str(tmp_path) in res["scope_hint"]

