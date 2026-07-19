"""从项目已有遥测实现学习可复用 contract，而非生成新规范。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", "coverage"}
_SENDER_MARKERS = (".pushLog(", ".pushEvent(", ".addEvent(", ".startActiveSpan(")
_TELEMETRY_IMPORT = re.compile(r"(?:@grafana/faro|@opentelemetry)", re.IGNORECASE)
_EXPORT_FUNCTION = re.compile(
    r"export\s+(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TelemetryContract:
    """由源码证据验证的项目遥测 helper。"""

    helper: Path
    export_name: str
    parameter_count: int
    required_parameters: int
    emitter: str
    evidence: str

    def call(self, event_name: str, signal: str,
             attributes: Optional[str] = None) -> Optional[str]:
        """仅在项目 helper 的现有签名兼容时生成调用。"""
        if self.parameter_count < 2:
            return None
        arguments = [event_name, signal]
        if attributes is not None and self.parameter_count >= 3:
            arguments.append(attributes)
        if self.required_parameters > len(arguments):
            return None
        return f"{self.export_name}({', '.join(arguments)})"


def _params(text: str) -> tuple[int, int]:
    values = [value.strip() for value in text.split(",") if value.strip()]
    required = sum("=" not in value and "?" not in value.split(":", 1)[0] for value in values)
    return len(values), required


def discover_frontend_contract(package_root: Path) -> Optional[TelemetryContract]:
    """发现项目自己的 wrapper；没有 wrapper 时拒绝自动补前端遥测。"""
    candidates = []
    for dirpath, dirnames, filenames in os.walk(package_root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not _TELEMETRY_IMPORT.search(source):
                continue
            sender_at = next((source.find(marker) for marker in _SENDER_MARKERS
                              if source.find(marker) >= 0), -1)
            if sender_at < 0:
                continue
            exports = [match for match in _EXPORT_FUNCTION.finditer(source)
                       if match.start() < sender_at]
            if not exports:
                continue
            match = exports[-1]
            parameter_count, required_parameters = _params(match.group("params"))
            emitter = "grafana-faro" if "@grafana/faro" in source else "opentelemetry"
            candidates.append(TelemetryContract(
                helper=path,
                export_name=match.group("name"),
                parameter_count=parameter_count,
                required_parameters=required_parameters,
                emitter=emitter,
                evidence=f"{path.name}: {match.group('name')} -> existing sender",
            ))
    compatible = [contract for contract in candidates
                  if contract.call('"event"', '"signal"', "{}")]
    return compatible[0] if compatible else None