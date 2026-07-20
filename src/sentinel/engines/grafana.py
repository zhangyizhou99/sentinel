"""Telemetry 计划与 Grafana dashboard 的生成/部署工具。"""
from __future__ import annotations

import json
import os
import re
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


# ---- dashboard 计划：基于【已埋点函数】而非盲区 -----------------------------
# dashboard 只能监控真正会发日志的函数。所以这里用 has_instrumentation，覆盖
# sentinel 补的（日志带 `sentinel:` 标记，可精确查）和项目本来就有的 log
# （回读源码提取日志字面量做锚点）。要哪些由用户用 targets 指定。

_LOG_CALL = re.compile(
    r"\.(?:info|warning|warn|error|debug|exception|trace|log|event|pushevent|pushlog)"
    r"\s*\(\s*(['\"`])(?P<text>.*?)\1",
    re.IGNORECASE | re.DOTALL,
)


def _static_prefix(text: str) -> str:
    """取字符串字面量中动态插值之前的静态前缀，作为 Loki 子串锚点。"""
    cut = len(text)
    for marker in ("{", "${", "%s", "%d", "%(", '" +', "' +", "`+", "\\n"):
        index = text.find(marker)
        if 0 <= index < cut:
            cut = index
    return text[:cut].strip()


def _escape_logql(value: str) -> str:
    """转义 Loki `|= "..."` 里的反斜杠与双引号，避免生成非法查询。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _instrumentation_anchor(repo: Path, unit) -> Dict[str, Any]:
    """回读已埋点函数源码，判断埋点来源并给出 Loki 查询锚点。

    - sentinel 补的：日志固定含 `sentinel: {qualname} touches …`，用它做精确锚点。
    - 项目本来的 log：提取第一条日志调用的字符串字面量（取静态前缀）做锚点。
    - 都取不到：无锚点，dashboard 退化为按函数名的宽松查询并标注不精确。
    """
    try:
        lines = (repo / unit.file).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {"source": "existing", "anchor": "", "precise": False}
    start = max((unit.start_line or 1) - 1, 0)
    end = unit.end_line or len(lines)
    body = "\n".join(lines[start:end])
    if f"sentinel: {unit.qualname} touches" in body:
        return {"source": "sentinel", "anchor": f"sentinel: {unit.qualname}", "precise": True}
    match = _LOG_CALL.search(body)
    if match:
        prefix = _static_prefix(match.group("text"))
        if len(prefix) >= 3:  # 太短的前缀匹配面过大，视作不精确
            return {"source": "existing", "anchor": prefix, "precise": True}
    return {"source": "existing", "anchor": "", "precise": False}


def _select_events(units, targets: str):
    """按 targets 从已埋点函数里挑（空/all=全部；数字=第 N 个；否则关键词/文件名子串匹配）。"""
    items = list(units)
    token = (targets or "").strip()
    if not token or token.lower() in ("all", "全部", "所有"):
        return items
    if token.isdigit():
        n = int(token)
        return items[n - 1:n] if 1 <= n <= len(items) else []
    keys = [k.strip().lower() for k in re.split(r"[,，、\s]+", token) if k.strip()]
    hit = [u for u in items if any(k in u.unit_id.lower() for k in keys)]
    if hit:
        return hit
    normalized = token.replace("\\", "/").lower()
    return [u for u in items if u.file.replace("\\", "/").lower().endswith(normalized)]


def plan_dashboard(repo: str, targets: str = "") -> Dict[str, Any]:
    """基于【已埋点函数】生成 dashboard 计划（sentinel 补的 + 项目本来就有的都算）。

    targets 留空=全部已埋点函数；否则按用户指定的函数名/关键词/文件名筛。
    每个事件带真实日志锚点（log_anchor），dashboard 用它对齐 Loki 查询。
    """
    target = Path(repo).resolve()
    result = scan_repo(str(target))
    instrumented = [u for u in result.units if u.has_instrumentation]
    selected = _select_events(instrumented, targets)
    events = []
    for unit in selected:
        event = f"{Path(unit.file).stem}.{unit.qualname}".replace(" ", "_").lower()
        anchor = _instrumentation_anchor(target, unit)
        events.append({
            "unit_id": unit.unit_id,
            "event": event,
            "qualname": unit.qualname,
            "signals": signals_of(unit),
            "language": unit.language,
            "source": anchor["source"],        # sentinel | existing
            "log_anchor": anchor["anchor"],    # Loki |= 用的真实日志子串（可能为空）
            "anchor_precise": anchor["precise"],
        })
    return {
        "repo": str(target),
        "service": _slug(target.name) or "service",
        "events": events,
        "count": len(events),
        "instrumented_total": len(instrumented),
        "note": ("基于已埋点函数生成；查询锚点取自真实日志文本。"
                 "标记 anchor_precise=false 的项需人工核对 LogQL。"
                 if selected else
                 "没有匹配的已埋点函数：先用 apply_instrumentation 补埋点，或换 targets。"),
    }


def generate_dashboard(plan: Dict[str, Any], datasource_uid: str = "") -> Dict[str, Any]:
    """生成可审阅的 Grafana dashboard JSON，不访问外部系统。

    每个面板的 LogQL 用事件自带的 log_anchor（真实日志子串）对齐；没有锚点时退化为
    按限定名的宽松匹配，并在面板描述里标注「不精确」，绝不假装能查到。
    """
    service = str(plan.get("service") or "service")
    datasource = datasource_uid or os.getenv("GRAFANA_LOKI_DATASOURCE_UID", "")
    title = f"Sentinel / {service} observability"
    panels = []
    imprecise = []
    for index, event in enumerate(plan.get("events") or []):
        anchor = event.get("log_anchor") or ""
        precise = bool(event.get("anchor_precise", False))
        selector = anchor or str(event.get("qualname") or event["event"])
        expr = f'{{service_name="{service}"}} |= "{_escape_logql(selector)}"'
        origin = "sentinel 补埋点" if event.get("source") == "sentinel" else "项目已有日志"
        desc = [f"来源: {origin}",
                f"信号: {', '.join(event.get('signals') or []) or '无'}",
                f"unit: {event['unit_id']}"]
        if not precise:
            desc.append("⚠️ 查询锚点不精确，部署前请人工核对 LogQL")
            imprecise.append(event["event"])
        panels.append({
            "id": index + 1,
            "title": event["event"],
            "type": "logs",
            "datasource": {"uid": datasource} if datasource else None,
            "targets": [{
                "refId": "A",
                "expr": expr,
                "legendFormat": event["event"],
            }],
            "description": "；".join(desc),
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
    notes = []
    if not datasource:
        notes.append("未配置 GRAFANA_LOKI_DATASOURCE_UID；部署前应绑定实际 Loki 数据源。")
    if imprecise:
        notes.append(f"{len(imprecise)} 个面板查询锚点不精确（{', '.join(imprecise)}），请人工核对。")
    return {
        "dashboard": dashboard,
        "datasource_configured": bool(datasource),
        "imprecise_panels": imprecise,
        "note": "；".join(notes) or "已绑定 datasource，且所有面板查询锚点均取自真实日志。",
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
    rel = payload.get("url") or ""
    full_url = f"{url}{rel}" if rel.startswith("/") else (rel or url)
    return {"ok": True, "uid": payload.get("uid"), "url": full_url, "status": payload.get("status")}