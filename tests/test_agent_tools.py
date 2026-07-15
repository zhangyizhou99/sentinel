"""scan / find_repo 工具封装测试（Tool ←→ scan/权限门 的桥）。"""
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
    # /etc 在范围外 → denied（且不会因不存在而抛错前就拦下）。
    res = tool.func("/etc")
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

