"""第 2 步：代码扫描器测试（纯静态，不联网、不用 LLM）。

运行：PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.scan import scan_file, scan_repo, signals_of  # noqa: E402

_FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "sample_app.py")


def _unit(units, qualname):
    return next(u for u in units if u.qualname == qualname)


def test_extracts_functions_and_methods():
    units = scan_file(_FIXTURE, "sample_app.py")
    names = {u.qualname for u in units}
    assert "create_order" in names          # 顶层函数
    assert "UserService.get_user" in names  # 类内方法（带类前缀）
    assert "add" in names


def test_captures_calls_and_signature():
    units = scan_file(_FIXTURE, "sample_app.py")
    co = _unit(units, "create_order")
    assert co.signature == "(order)"
    # 抓到了 redis 和 http 调用（点号名）
    joined = " ".join(co.calls).lower()
    assert "get" in joined and "post" in joined


def test_signals_detected():
    units = scan_file(_FIXTURE, "sample_app.py")
    assert set(signals_of(_unit(units, "create_order"))) >= {"cache", "http"}
    assert signals_of(_unit(units, "add")) == []  # 纯计算无信号


def test_instrumentation_detection():
    units = scan_file(_FIXTURE, "sample_app.py")
    assert _unit(units, "create_order").has_instrumentation is False  # 没打 log
    assert _unit(units, "UserService.get_user").has_instrumentation is True  # 有 logger


def test_blind_spots():
    res = scan_repo(_FIXTURE)
    blind = {u.qualname for u in res.blind_spots}
    assert "create_order" in blind             # 盲区：调依赖没埋点
    assert "UserService.get_user" not in blind  # 有埋点，不算盲区
    assert "add" not in blind                   # 无信号，不算盲区


def test_scan_repo_skips_and_survives_bad_files(tmp_path):
    # 正常文件 + 语法错误文件 + 应跳过的目录，扫描不崩，只收正常单元。
    (tmp_path / "good.py").write_text("import redis\ndef f():\n    return redis.Redis().get('k')\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")  # 故意语法错误
    skip = tmp_path / "node_modules"
    skip.mkdir()
    (skip / "x.py").write_text("def should_be_skipped(): pass\n")

    res = scan_repo(str(tmp_path))
    names = {u.qualname for u in res.units}
    assert "f" in names
    assert "should_be_skipped" not in names  # 被跳过目录
    # bad.py 解析失败被容错跳过，不影响 good.py


def test_content_hash_stable_and_id():
    units = scan_file(_FIXTURE, "sample_app.py")
    co = _unit(units, "create_order")
    assert co.unit_id == "sample_app.py::create_order"
    assert len(co.content_hash) == 16
