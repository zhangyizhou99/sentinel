"""补埋点应用引擎测试（DESIGN §8.3）。用临时 git 仓库验证真实改写 + 安全。"""
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.apply import Applier, ApplyError  # noqa: E402
from sentinel.engines.scan import scan_repo  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo():
    d = Path(tempfile.mkdtemp())
    (d / "svc.py").write_text("def checkin():\n    x = redis.get('k')\n    return x\n")
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "init")
    return d


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_apply_creates_branch_with_uncommitted_edits():
    d = _make_repo()
    blind = scan_repo(str(d)).blind_spots
    assert blind                                            # checkin 是盲区
    out = Applier().apply(str(d), blind, "sentinel/fix")
    assert "svc.py" in out.files_changed
    assert any("checkin" in uid for uid in out.units_fixed)
    assert _head(d) == "sentinel/fix"                       # 停在新分支
    status = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status.strip()                                   # 改动未提交
    txt = (d / "svc.py").read_text()
    assert "logging.getLogger(__name__).info" in txt        # 真的插了埋点
    ast.parse(txt)                                          # 仍能解析
    assert "logging" in out.diff


def test_apply_requires_clean_tree():
    d = _make_repo()
    (d / "dirty.txt").write_text("x")                       # 弄脏工作区
    blind = scan_repo(str(d)).blind_spots
    try:
        Applier().apply(str(d), blind, "b")
        assert False, "应因工作区不干净而报错"
    except ApplyError as e:
        assert "干净" in str(e) or "clean" in str(e)


def test_apply_rejects_existing_branch():
    d = _make_repo()
    _git(d, "branch", "taken")
    blind = scan_repo(str(d)).blind_spots
    try:
        Applier().apply(str(d), blind, "taken")
        assert False, "应因分支已存在而报错"
    except ApplyError as e:
        assert "已存在" in str(e) or "exists" in str(e)


def test_apply_records_reusable_skill():
    from sentinel.memory.procedural import ProceduralMemory
    d = _make_repo()
    pm = ProceduralMemory(str(Path(tempfile.mkdtemp()) / "sk.db"))
    blind = scan_repo(str(d)).blind_spots
    Applier().apply(str(d), blind, "sk", procedural=pm)
    # checkin 触及 redis(cache) → 记录 (python, cache) 修复技能，供同类盲区复用
    assert pm.get_skill("python", "cache") is not None
