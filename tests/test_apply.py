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


def _add_faro_setup(repo: Path, sender="pushLog"):
    (repo / "package.json").write_text(
        '{"dependencies":{"@grafana/faro-web-sdk":"^2.8.2"}}', encoding="utf-8")
    helper = repo / "observability" / "events.ts"
    helper.parent.mkdir()
    sender_call = (
        "  faro.api.pushEvent(name, { signal, ...attributes })\n"
        if sender == "pushEvent" else
        "  faro.api.pushLog([name], { context: { signal } })\n"
    )
    helper.write_text(
        "import { initializeFaro } from '@grafana/faro-web-sdk'\n"
        "const faro = initializeFaro({ url: import.meta.env.VITE_GRAFANA_FARO_URL })\n"
        "type Attributes = Record<string, string>\n"
        "export function recordObservability(\n"
        "  name: string, signal: string, attributes: Attributes = {},\n"
        "): void {\n"
        + sender_call +
        "}\n",
        encoding="utf-8",
    )


def _make_typescript_repo(with_faro=True, receiver_configured=False, sender="pushLog"):
    from sentinel.scanners.treesitter_scanner import register_builtin_languages

    register_builtin_languages()
    d = Path(tempfile.mkdtemp())
    (d / "queue.ts").write_text(
        "export async function flush(): Promise<void> {\n"
        "  await fetch('/api/flush')\n"
        "}\n",
        encoding="utf-8",
    )
    if with_faro:
        _add_faro_setup(d, sender=sender)
        if receiver_configured:
            (d / ".env.local").write_text(
                "VITE_GRAFANA_FARO_URL=https://faro.example.test/collect\n", encoding="utf-8")
    else:
        (d / "package.json").write_text("{}", encoding="utf-8")
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


def test_apply_reflection_preserves_unselected_blind_spots():
    from sentinel.engines.agent_tools import build_apply_tool

    d = _make_repo()
    with (d / "svc.py").open("a", encoding="utf-8") as source:
        source.write("\ndef load_cargo():\n    return db.query('cargo')\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "add second blind spot")

    out = build_apply_tool().func({
        "repo": str(d), "targets": "checkin", "branch": "reflect",
    })["applied"]

    reflection = out["reflection"]
    assert reflection["passed"] is True
    assert reflection["syntax_passed"] is True
    assert reflection["selected_resolved"] == ["svc.py::checkin"]
    assert reflection["unselected_preserved"] == ["svc.py::load_cargo"]
    assert reflection["unexpected_resolved"] == []
    assert reflection["new_blind_spots"] == []


def test_apply_tool_uses_available_suffix_when_branch_exists():
    from sentinel.engines.agent_tools import build_apply_tool

    d = _make_repo()
    _git(d, "branch", "fix-observability")

    out = build_apply_tool().func({
        "repo": str(d), "targets": "checkin", "branch": "fix-observability",
    })

    assert out["applied"]["branch"] == "fix-observability-2"


def test_select_targets_expands_file_name_to_all_file_blind_spots():
    from sentinel.engines.agent_tools import _select_targets
    from sentinel.model.code_unit import CodeUnit

    spots = [
        CodeUnit(file="src/offline/queue.ts", qualname="read", kind="function", signature="()"),
        CodeUnit(file="src/offline/queue.ts", qualname="write", kind="function", signature="()"),
        CodeUnit(file="src/api/client.ts", qualname="request", kind="function", signature="()"),
    ]

    selected = _select_targets(spots, "queue.ts")

    assert [unit.unit_id for unit in selected] == [
        "src/offline/queue.ts::read", "src/offline/queue.ts::write",
    ]


def test_apply_instruments_typescript_and_rescan_recognizes_it():
    from sentinel.scanners import base

    registry_before = dict(base._REGISTRY)
    try:
        d = _make_typescript_repo()
        blind = scan_repo(str(d)).blind_spots
        assert [unit.unit_id for unit in blind] == ["queue.ts::flush"]

        out = Applier().apply(str(d), blind, "sentinel/fix-ts")

        text = (d / "queue.ts").read_text(encoding="utf-8")
        assert "// sentinel: observability" in text
        assert 'recordObservability("queue.flush", "http", { phase: \'start\' })' in text
        assert "import { recordObservability } from './observability/events'" in text
        assert out.units_fixed == ["queue.ts::flush"]
        assert out.emitter == "grafana-faro"
        assert out.receiver_configured is False
        assert out.delivery == "pending_configuration"
        assert scan_repo(str(d)).blind_spots == []
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(registry_before)


def test_apply_instruments_typescript_with_faro_push_event():
    from sentinel.scanners import base

    registry_before = dict(base._REGISTRY)
    try:
        d = _make_typescript_repo(sender="pushEvent")
        blind = scan_repo(str(d)).blind_spots

        out = Applier().apply(str(d), blind, "sentinel/fix-push-event")

        text = (d / "queue.ts").read_text(encoding="utf-8")
        assert 'recordObservability("queue.flush", "http", { phase: \'start\' })' in text
        assert out.emitter == "grafana-faro"
        assert scan_repo(str(d)).blind_spots == []
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(registry_before)


def test_apply_reports_configured_faro_delivery_as_unverified():
    from sentinel.scanners import base

    registry_before = dict(base._REGISTRY)
    try:
        d = _make_typescript_repo(receiver_configured=True)
        blind = scan_repo(str(d)).blind_spots

        out = Applier().apply(str(d), blind, "sentinel/faro-configured")

        assert out.receiver_configured is True
        assert out.delivery == "configured_unverified"
        assert "真实网络投递验证" in out.delivery_note
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(registry_before)


def test_apply_rejects_go_without_verified_telemetry_emitter():
    from sentinel.model.code_unit import CodeUnit

    d = Path(tempfile.mkdtemp())
    (d / "main.go").write_text(
        "package main\n\nfunc load() {\n  fetch()\n}\n",
        encoding="utf-8",
    )
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "init")
    unit = CodeUnit(file="main.go", qualname="load", kind="function", signature="()",
                    calls=["fetch"], start_line=3, end_line=5, language="go")

    try:
        Applier().apply(str(d), [unit], "sentinel/fix-go")
        assert False, "无真实 emitter 时不应插 println"
    except ApplyError as error:
        assert "telemetry emitter" in str(error)

    assert "println" not in (d / "main.go").read_text(encoding="utf-8")
    assert _head(d) != "sentinel/fix-go"


def test_apply_instruments_multiple_typescript_units_without_line_drift():
    from sentinel.memory.procedural import ProceduralMemory
    from sentinel.scanners import base
    from sentinel.scanners.treesitter_scanner import register_builtin_languages

    registry_before = dict(base._REGISTRY)
    try:
        register_builtin_languages()
        d = Path(tempfile.mkdtemp())
        (d / "queue.ts").write_text(
            "export async function first() {\n  await fetch('/first')\n}\n\n"
            "export async function second() {\n  await fetch('/second')\n}\n",
            encoding="utf-8",
        )
        _add_faro_setup(d)
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
        _git(d, "add", ".")
        _git(d, "commit", "-qm", "init")
        memory = ProceduralMemory(str(Path(tempfile.mkdtemp()) / "skills.db"))
        blind = scan_repo(str(d)).blind_spots

        result = Applier().apply(str(d), blind, "sentinel/fix-many", procedural=memory)

        text = (d / "queue.ts").read_text(encoding="utf-8")
        assert text.count("// sentinel: observability") == 2
        assert 'recordObservability("queue.first", "http", { phase: \'start\' })' in text
        assert 'recordObservability("queue.second", "http", { phase: \'start\' })' in text
        assert text.count("import { recordObservability }") == 1
        assert len(result.units_fixed) == 2
        assert memory.get_skill("typescript", "http") is None
        assert scan_repo(str(d)).blind_spots == []
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(registry_before)


def test_apply_rejects_typescript_without_faro_before_creating_branch():
    from sentinel.scanners import base

    registry_before = dict(base._REGISTRY)
    try:
        d = _make_typescript_repo(with_faro=False)
        blind = scan_repo(str(d)).blind_spots

        try:
            Applier().apply(str(d), blind, "sentinel/no-faro")
            assert False, "缺少 Faro SDK 时不应回退到 console.info"
        except ApplyError as error:
            assert "@grafana/faro-web-sdk" in str(error)

        assert "console.info" not in (d / "queue.ts").read_text(encoding="utf-8")
        assert _head(d) != "sentinel/no-faro"
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(registry_before)


def test_apply_without_valid_candidate_does_not_create_branch():
    from sentinel.model.code_unit import CodeUnit

    d = Path(tempfile.mkdtemp())
    (d / "main.odd").write_text("fn load() {\n  fetch()\n}\n", encoding="utf-8")
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "init")
    unit = CodeUnit(file="main.odd", qualname="load", kind="function", signature="()",
                    calls=["fetch"], start_line=1, end_line=3, language="unregistered")

    try:
        Applier().apply(str(d), [unit], "sentinel/no-candidate")
        assert False, "无可验证候选时应拒绝"
    except ApplyError as error:
        assert "没有生成" in str(error)

    branch = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--verify", "sentinel/no-candidate"],
        capture_output=True, text=True,
    )
    assert branch.returncode != 0
