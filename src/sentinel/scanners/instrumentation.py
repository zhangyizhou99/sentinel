"""埋点判据 —— 多语言「是否已经埋点」的判断。

放在独立小模块（不 import 任何 scanner / engine），避免 scan.py 与各语言
解析器之间的循环导入。判据基于子串：遵循三层召回漏斗的 L1（确定性）。
注意：**不把 `console.log` / `console.info` 当埋点**（它们是本地控制台输出，不代表
遥测已进入后端）；否则会掩盖真正的投递盲区。console.error/warn 仍作为错误日志识别。
"""
from __future__ import annotations

from typing import Dict, Tuple

# 跨语言共享的遥测词（logger/otel/prometheus 等在多数生态里同名）。
_COMMON: Tuple[str, ...] = (
    "recordobservability", "pushevent", "@grafana/faro", "initializefaro",
    "logger", "logging", "getlogger", ".log(", "log.info", "log.error",
    "metrics", "meter", "counter", "histogram", "gauge",
    "tracer", "span", "otel", "opentelemetry", "statsd", "prometheus",
    "sentry", "captureexception", "capturemessage", "datadog",
)

# 语言专属埋点写法。
_BY_LANG: Dict[str, Tuple[str, ...]] = {
    "python": ("structlog",),
    "javascript": ("console.error", "console.warn", "analytics", "track(",
                   "segment", "amplitude", "mixpanel", "posthog", "newrelic",
                   "pino", "winston", "bunyan", "logtail", "dd-trace"),
    "go": ("slog", "zap", "logrus", "zerolog"),
}
_BY_LANG["typescript"] = _BY_LANG["javascript"]
_BY_LANG["tsx"] = _BY_LANG["javascript"]

# 向后兼容 + 未知语言兵底：所有提示词的并集（宽松判定）。
INSTRUMENTATION_HINTS: Tuple[str, ...] = tuple(sorted(set(
    _COMMON + sum(_BY_LANG.values(), ()) + ("console.log",))))


def has_instrumentation(blob: str, language: str = "") -> bool:
    """给定一段文本（会小写化后匹配）与语言，判断是否已埋点。

    未传语言（或未知）时用全并集宽松判定；传了语言则用「该语言专属 + 跨语言共享」。
    """
    low = (blob or "").lower()
    lang = (language or "").lower()
    if not lang:
        hints: Tuple[str, ...] = INSTRUMENTATION_HINTS
    else:
        hints = _BY_LANG.get(lang, ()) + _COMMON
    return any(h in low for h in hints)
