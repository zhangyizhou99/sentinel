"""Telemetry 计划与 Grafana dashboard 的生成/部署工具。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sentinel.engines.scan import scan_repo, signals_of


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def generate_telemetry_plan(repo: str) -> Dict[str, Any]:
    """生成不落盘的遥测计划；不能替代 apply，也不声称事件已投递。"""
    target = Path(repo).resolve()
    result = scan_repo(str(target))
    events = []
    for unit in result.blind_spots:
        event = f"{Path(unit.file).stem}.{unit.qualname}".replace(" ", "_").lower()
        events.append({
            "unit_id": unit.unit_id,
            "event": event,
            "signals": signals_of(unit),
            "language": unit.language,
        })
    return {
        "repo": str(target),
        "service": _slug(target.name) or "service",
        "events": events,
        "count": len(events),
        "note": "这是遥测计划；需 apply_instrumentation 写入 emitter，随后部署 dashboard。",
    }


def generate_dashboard(plan: Dict[str, Any], datasource_uid: str = "") -> Dict[str, Any]:
    """生成可审阅的 Grafana dashboard JSON，不访问外部系统。"""
    service = str(plan.get("service") or "service")
    datasource = datasource_uid or os.getenv("GRAFANA_LOKI_DATASOURCE_UID", "")
    title = f"Sentinel / {service} observability"
    panels = []
    for index, event in enumerate(plan.get("events") or []):
        panels.append({
            "id": index + 1,
            "title": event["event"],
            "type": "logs",
            "datasource": {"uid": datasource} if datasource else None,
            "targets": [{
                "refId": "A",
                "expr": f'{{service_name="{service}"}} |= "sentinel {event["event"]}"',
                "legendFormat": event["event"],
            }],
            "description": f"Signals: {', '.join(event['signals']) or 'unknown'}; unit: {event['unit_id']}",
        })
    dashboard = {
        "uid": f"sentinel-{service}"[:40],
        "title": title,
        "tags": ["sentinel", "observability", service],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "30s",
        "panels": panels,
    }
    return {
        "dashboard": dashboard,
        "datasource_configured": bool(datasource),
        "note": ("已绑定 datasource UID。" if datasource else
                 "未配置 GRAFANA_LOKI_DATASOURCE_UID；JSON 已生成，但部署前应绑定实际 Loki 数据源。"),
    }


def deploy_dashboard(dashboard: Dict[str, Any], folder_uid: str = "") -> Dict[str, Any]:
    """通过 Grafana HTTP API 幂等 upsert dashboard，凭据只读自环境变量。"""
    url = os.getenv("GRAFANA_URL", "").rstrip("/")
    token = os.getenv("GRAFANA_TOKEN", "")
    if not url or not token:
        return {
            "ok": False,
            "reason": "缺少 GRAFANA_URL 或 GRAFANA_TOKEN；未发起部署。",
            "required": ["GRAFANA_URL", "GRAFANA_TOKEN"],
        }
    body = {
        "dashboard": dashboard,
        "folderUid": folder_uid or os.getenv("GRAFANA_FOLDER_UID", ""),
        "overwrite": True,
    }
    request = Request(
        f"{url}/api/dashboards/db",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:  # nosec B310 - URL is explicit operator config
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return {"ok": False, "reason": f"Grafana API 返回 HTTP {error.code}", "body": error.read().decode("utf-8", "replace")}
    except (URLError, ValueError, OSError) as error:
        return {"ok": False, "reason": f"无法部署到 Grafana：{error}"}
    return {"ok": True, "uid": payload.get("uid"), "url": payload.get("url"), "status": payload.get("status")}