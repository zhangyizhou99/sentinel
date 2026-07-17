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


def test_apply_follows_structlog_convention():
    """apply 按项目约定风格补：structlog 项目 → 生成 structlog 埋点（入乡随俗）。"""
    from sentinel.engines.conventions import InstrumentationConvention
    d = _make_repo()
    conv = InstrumentationConvention(repo=str(d), style="structlog",
                                     top_calls=["log.info"], sample_count=3)
    blind = scan_repo(str(d)).blind_spots
    Applier().apply(str(d), blind, "sl", convention=conv)
    txt = (d / "svc.py").read_text()
    assert "import structlog" in txt
    assert "structlog.get_logger().info" in txt
    ast.parse(txt)


def test_apply_tool_executes_selected_target():
    """apply 工具是结构化执行器：按 targets 只补选中的盲区（意图理解在上游由 LLM 完成）。"""
    from sentinel.engines.agent_tools import build_apply_tool
    d = _make_repo()   # svc.py: checkin(redis/cache) + load_cargo(db)
    out = build_apply_tool().func({"repo": str(d), "targets": "checkin", "branch": "sel"})
    applied = out["applied"]
    assert applied["units_fixed"] == ["svc.py::checkin"]     # 只补了 checkin
    txt = (d / "svc.py").read_text()
    assert "checkin touches" in txt
    assert "load_cargo touches" not in txt                   # 没补 load_cargo
    ast.parse(txt)


def test_apply_rejects_non_python_before_branching():
    """全是非 Python 盲区 → 提前拒绝，不建空分支（避免'切了分支啥也没补'的困惑）。"""
    from sentinel.model.code_unit import CodeUnit
    d = _make_repo()
    ts = CodeUnit(file="app.ts", qualname="f", kind="function", signature="()",
                  calls=["fetch"], has_instrumentation=False, language="typescript")
    try:
        Applier().apply(str(d), [ts], "b1")
        assert False, "应因非 Python 而拒绝"
    except ApplyError as e:
        assert "Python" in str(e) or "只支持" in str(e)
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "--verify", "b1"],
                       capture_output=True, text=True)
    assert r.returncode != 0                             # 关键：没建分支 b1
