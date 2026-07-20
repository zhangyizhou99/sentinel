"""Telemetry 计划与 Grafana dashboard 生成/部署测试。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines import grafana  # noqa: E402


def test_generate_dashboard_uses_real_log_anchor():
    plan = {"repo": "d:/repo", "service": "haulhero", "events": [{
        "unit_id": "src/queue.ts::flush", "event": "queue.flush", "qualname": "flush",
        "signals": ["http"], "language": "typescript",
        "source": "sentinel", "log_anchor": "sentinel: flush", "anchor_precise": True,
    }]}

    result = grafana.generate_dashboard(plan)

    assert result["datasource_configured"] is False
    assert result["imprecise_panels"] == []
    assert result["dashboard"]["uid"] == "sentinel-haulhero"
    panel = result["dashboard"]["panels"][0]
    assert panel["type"] == "logs"
    # 查询锚点必须等于真实日志子串，而不是拼出来的 event 名
    assert panel["targets"][0]["expr"] == '{service_name="haulhero"} |= "sentinel: flush"'


def test_generate_dashboard_flags_imprecise_anchor():
    plan = {"service": "svc", "events": [{
        "unit_id": "a.py::run", "event": "a.run", "qualname": "run",
        "signals": [], "language": "python",
        "source": "existing", "log_anchor": "", "anchor_precise": False,
    }]}

    result = grafana.generate_dashboard(plan)

    assert result["imprecise_panels"] == ["a.run"]
    panel = result["dashboard"]["panels"][0]
    assert '|= "run"' in panel["targets"][0]["expr"]  # 无锚点时退化为限定名
    assert "不精确" in panel["description"]


def test_deploy_dashboard_requires_explicit_credentials(monkeypatch):
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    monkeypatch.delenv("GRAFANA_TOKEN", raising=False)

    result = grafana.deploy_dashboard({"uid": "sentinel-test", "title": "Test"})

    assert result["ok"] is False
    assert "GRAFANA_URL" in result["reason"]


def test_deploy_dashboard_posts_idempotent_upsert(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"uid":"sentinel-test","url":"/d/sentinel-test","status":"success"}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("GRAFANA_URL", "https://grafana.example.test/")
    monkeypatch.setenv("GRAFANA_TOKEN", "secret")
    monkeypatch.setattr(grafana, "urlopen", fake_open)

    result = grafana.deploy_dashboard({"uid": "sentinel-test", "title": "Test"}, "sentinel")

    assert result == {"ok": True, "uid": "sentinel-test",
                      "url": "https://grafana.example.test/d/sentinel-test", "status": "success"}
    assert captured["url"] == "https://grafana.example.test/api/dashboards/db"
    assert captured["body"]["overwrite"] is True
    assert captured["body"]["folderUid"] == "sentinel"


_SAMPLE_REPO = (
    "import logging\n"
    "import requests\n"
    "\n"
    "def flush():\n"
    "    logging.getLogger(__name__).info('sentinel: flush touches http')\n"
    "    return requests.get('https://x').text\n"
    "\n"
    "def save_order():\n"
    "    logging.getLogger(__name__).info('order created id=%s', 1)\n"
    "    return requests.post('https://x')\n"
    "\n"
    "def blind_call():\n"
    "    return requests.get('https://y').json()\n"
)


def test_plan_dashboard_only_includes_instrumented(tmp_path):
    (tmp_path / "svc.py").write_text(_SAMPLE_REPO, encoding="utf-8")

    plan = grafana.plan_dashboard(str(tmp_path))
    events = {event["qualname"]: event for event in plan["events"]}

    # 已埋点的进；纯盲区（只调依赖没日志）不进
    assert "flush" in events
    assert "save_order" in events
    assert "blind_call" not in events
    # sentinel 补的 → 精确锚点；项目本来的 log → 提取字面量静态前缀
    assert events["flush"]["source"] == "sentinel"
    assert events["flush"]["log_anchor"] == "sentinel: flush"
    assert events["flush"]["anchor_precise"] is True
    assert events["save_order"]["source"] == "existing"
    assert events["save_order"]["log_anchor"] == "order created id="
    assert events["save_order"]["anchor_precise"] is True


def test_plan_dashboard_targets_filter(tmp_path):
    (tmp_path / "svc.py").write_text(_SAMPLE_REPO, encoding="utf-8")

    plan = grafana.plan_dashboard(str(tmp_path), "flush")

    assert [event["qualname"] for event in plan["events"]] == ["flush"]


def test_deploy_tool_requires_approval():
    from sentinel.engines.agent_tools import build_deploy_dashboard_tool

    tool = build_deploy_dashboard_tool()
    result = tool.func({"repo": "/tmp/whatever"})

    assert result["ok"] is False
    assert "同意" in result["reason"] or "approved" in result["reason"]


def test_deploy_tool_generates_and_deploys_only_selected(tmp_path, monkeypatch):
    from sentinel.engines import agent_tools

    (tmp_path / "svc.py").write_text(_SAMPLE_REPO, encoding="utf-8")
    captured = {}

    def fake_deploy(dashboard, folder_uid=""):
        captured["dashboard"] = dashboard
        return {"ok": True, "uid": "sentinel-x", "url": "/d/x", "status": "success"}

    monkeypatch.setattr("sentinel.engines.grafana.deploy_dashboard", fake_deploy)
    tool = agent_tools.build_deploy_dashboard_tool()

    result = tool.func({"repo": str(tmp_path), "targets": "flush", "approved": True})

    assert result["ok"] is True
    # agent 只报了 repo+targets，工具内部自己生成并只部署选中的 flush
    assert result["panels"] == ["svc.flush"]
    assert len(captured["dashboard"]["panels"]) == 1


def test_deploy_tool_empty_targets_lists_candidates_not_deploy(tmp_path, monkeypatch):
    from sentinel.engines import agent_tools

    (tmp_path / "svc.py").write_text(_SAMPLE_REPO, encoding="utf-8")
    called = {"deployed": False}

    def fake_deploy(dashboard, folder_uid=""):
        called["deployed"] = True
        return {"ok": True}

    monkeypatch.setattr("sentinel.engines.grafana.deploy_dashboard", fake_deploy)
    tool = agent_tools.build_deploy_dashboard_tool()

    # 没指定 targets：不应部署，而是列出候选让用户选
    result = tool.func({"repo": str(tmp_path), "approved": True})

    assert result["ok"] is False
    assert result["needs_targets"] is True
    assert "svc.flush" in result["candidates"]
    assert called["deployed"] is False

