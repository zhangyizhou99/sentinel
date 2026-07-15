"""权限中枢测试（PermissionBroker）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.permissions import PermissionBroker  # noqa: E402

_ROOT = Path(__file__).resolve().parent  # tests/


def test_within_scope():
    b = PermissionBroker(str(_ROOT))
    assert b.within_scope(str(_ROOT / "fixtures"))
    assert b.within_scope(str(_ROOT))              # 根本身
    assert not b.within_scope("/etc/passwd")       # 范围外


def test_grant_and_is_granted():
    b = PermissionBroker(str(_ROOT))
    target = str(_ROOT / "fixtures")
    assert not b.is_granted(target)
    b.grant(target)
    assert b.is_granted(target)
    assert b.is_granted(str(_ROOT / "fixtures" / "sample_app.py"))  # 子路径被覆盖


def test_grant_out_of_scope_refused():
    b = PermissionBroker(str(_ROOT))
    with pytest.raises(PermissionError):
        b.grant("/etc")            # 不可提权到范围外
