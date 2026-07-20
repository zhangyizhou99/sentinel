"""git 增量扫描测试（Drift · roadmap 第 6 步）。用临时 git 仓库验证。"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.gitscan import (  # noqa: E402
    GitScanError,
    resolve_base,
    scan_changed_repo,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_main():
    """建仓：main 上有一个盲区函数 alpha（db）。"""
    d = Path(tempfile.mkdtemp())
    (d / "svc.py").write_text("def alpha():\n    return db.query('k')\n")
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "base")
    _git(d, "branch", "-M", "main")
    return d


def test_scan_changed_reports_only_touched_functions():
    d = _make_main()
    _git(d, "checkout", "-qb", "feature")
    with (d / "svc.py").open("a") as f:               # 追加新盲区 beta，不动 alpha
        f.write("\ndef beta():\n    return redis.get('k')\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "add beta")

    result, base = scan_changed_repo(str(d), "main")
    ids = {u.unit_id for u in result.blind_spots}
    assert ids == {"svc.py::beta"}                    # 只报改动到的 beta，不报未动的 alpha
    assert base


def test_scan_changed_reports_modified_function():
    d = _make_main()
    _git(d, "checkout", "-qb", "feature")
    (d / "svc.py").write_text("def alpha():\n    y = 1\n    return db.query('k')\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "touch alpha")

    result, _ = scan_changed_repo(str(d), "main")
    assert {u.unit_id for u in result.blind_spots} == {"svc.py::alpha"}


def test_scan_changed_includes_uncommitted():
    d = _make_main()
    _git(d, "checkout", "-qb", "feature")
    with (d / "svc.py").open("a") as f:               # 未提交
        f.write("\ndef gamma():\n    return redis.get('k')\n")

    result, _ = scan_changed_repo(str(d), "main")
    assert "svc.py::gamma" in {u.unit_id for u in result.blind_spots}


def test_scan_changed_includes_untracked_new_file():
    d = _make_main()
    (d / "new.py").write_text("def delta():\n    return session.execute('q')\n")  # 未 add

    result, _ = scan_changed_repo(str(d), "main")
    assert "new.py::delta" in {u.unit_id for u in result.blind_spots}


def test_scan_changed_requires_git_repo():
    d = Path(tempfile.mkdtemp())
    (d / "x.py").write_text("def a():\n    return db.query('k')\n")
    try:
        scan_changed_repo(str(d), None)
        assert False, "非 git 仓库应报错"
    except GitScanError as e:
        assert "git" in str(e).lower() or "仓库" in str(e)


def test_resolve_base_falls_back_to_master():
    d = Path(tempfile.mkdtemp())
    (d / "svc.py").write_text("def alpha():\n    return db.query('k')\n")
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "base")
    _git(d, "branch", "-M", "master")                 # 只有 master，没有 main

    base = resolve_base(str(d), None)                 # 默认 main 不存在 → 退 master
    assert base


def test_scan_changed_unknown_base_raises():
    d = _make_main()
    try:
        scan_changed_repo(str(d), "nonexistent-branch")
        assert False, "找不到基准分支应报错"
    except GitScanError as e:
        assert "base" in str(e).lower() or "基准" in str(e)
