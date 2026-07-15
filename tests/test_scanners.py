"""可插拔语言解析器测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.scanners import get_scanner_for, supported_extensions  # noqa: E402
from sentinel.scanners.python_scanner import PythonScanner  # noqa: E402


def test_python_registered():
    assert ".py" in supported_extensions()
    assert isinstance(get_scanner_for("a/b/c.py"), PythonScanner)


def test_unknown_extension_has_no_scanner():
    assert get_scanner_for("app.js") is None   # 还没注册 JS 后端
    assert get_scanner_for("README.md") is None
    assert get_scanner_for("noext") is None
