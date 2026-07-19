"""Telemetry 计划与 Grafana dashboard 生成/部署测试。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines import grafana  # noqa: E402


def test_generate_dashboard_marks_missing_datasource():
    plan = {"repo": "d:/repo", "service": "haulhero", "events": [{
        "unit_id": "src/queue.ts::flush", "event": "queue.flush", "signals": ["http"],
        "language": "typescript",
    }]}

    result = grafana.generate_dashboard(plan)

    assert result["datasource_configured"] is False
    assert result["dashboard"]["uid"] == "sentinel-haulhero"
    panel = result["dashboard"]["panels"][0]
    assert panel["type"] == "logs"
    assert 'service_name="haulhero"' in panel["targets"][0]["expr"]
    assert 'sentinel queue.flush' in panel["targets"][0]["expr"]


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

    assert result == {"ok": True, "uid": "sentinel-test", "url": "/d/sentinel-test", "status": "success"}
    assert captured["url"] == "https://grafana.example.test/api/dashboards/db"
    assert captured["body"]["overwrite"] is True
    assert captured["body"]["folderUid"] == "sentinel"