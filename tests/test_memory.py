"""情节记忆 + 反馈学习测试。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.memory import EpisodicMemory, IGNORE, INSTRUMENT  # noqa: E402
from sentinel.engines.agent_tools import build_scan_tool, build_feedback_tool  # noqa: E402


def _mem() -> EpisodicMemory:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # 让 EpisodicMemory 自己新建
    return EpisodicMemory(db_path=path)


def _repo_with_blind_spot() -> str:
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "svc.py"), "w", encoding="utf-8") as f:
        f.write("import redis\n\n"
                "def create_order(o):\n"
                "    return redis.Redis().get(o)\n")
    return d


def test_record_and_list_runs():
    m = _mem()
    rid = m.record_run("/tmp/repoA", blind_spot_count=3, suppressed_count=1)
    assert rid >= 1
    runs = m.list_runs("/tmp/repoA")
    assert len(runs) == 1 and runs[0].blind_spot_count == 3 and runs[0].suppressed_count == 1
    assert m.last_repo == os.path.abspath("/tmp/repoA")
    m.close()


def test_feedback_upsert_and_ignored():
    m = _mem()
    m.record_feedback("/tmp/r", "svc.py::f", IGNORE)
    assert m.is_ignored("/tmp/r", "svc.py::f")
    assert m.ignored_units("/tmp/r") == {"svc.py::f"}
    # 改判为 instrument → 覆盖，不再算忽略
    m.record_feedback("/tmp/r", "svc.py::f", INSTRUMENT)
    assert not m.is_ignored("/tmp/r", "svc.py::f")
    assert m.ignored_units("/tmp/r") == set()
    assert len(m.list_feedback("/tmp/r")) == 1  # upsert 不产生重复行
    m.close()


def test_bad_decision_raises():
    m = _mem()
    try:
        m.record_feedback("/tmp/r", "x::y", "maybe")
        assert False, "应当拒绝非法 decision"
    except ValueError:
        pass
    m.close()


def test_scan_tool_suppresses_ignored():
    m = _mem()
    repo = _repo_with_blind_spot()
    tool = build_scan_tool(broker=None, memory=m)

    r1 = tool.func(repo)
    assert r1["blind_spot_count"] == 1 and r1["suppressed_count"] == 0
    uid = r1["blind_spots"][0]["unit_id"]
    assert uid == "svc.py::create_order"

    m.record_feedback(repo, uid, IGNORE)          # 用户：这个不用埋点
    r2 = tool.func(repo)
    assert r2["blind_spot_count"] == 0             # 被抑制
    assert r2["suppressed_count"] == 1
    # 运行都被记录（两次 scan）
    assert len(m.list_runs(repo)) == 2
    m.close()


def test_feedback_tool_uses_last_repo():
    m = _mem()
    repo = _repo_with_blind_spot()
    build_scan_tool(broker=None, memory=m).func(repo)   # 设置 last_repo
    fb = build_feedback_tool(m)
    out = fb.func("svc.py::create_order")
    assert out["ok"] is True
    assert m.is_ignored(repo, "svc.py::create_order")
    m.close()


def test_feedback_tool_requires_prior_scan():
    m = _mem()
    fb = build_feedback_tool(m)
    out = fb.func("svc.py::create_order")               # 还没扫过任何仓库
    assert "error" in out
    m.close()
