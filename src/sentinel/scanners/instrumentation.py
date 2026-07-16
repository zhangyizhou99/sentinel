"""埋点判据 —— 跨语言共享的「是否已经埋点」判断。

放在独立小模块（不 import 任何 scanner / engine），避免 scan.py 与各语言
解析器之间的循环导入。判据基于子串：logger/otel/prometheus 等在多数生态里同名，
所以一套子串能覆盖 Python/JS/Go/... 常见埋点写法。
"""
from __future__ import annotations

# 「函数体/调用名里出现这些子串」→ 认为已埋点。
INSTRUMENTATION_HINTS = (
    "logger", "logging", "getlogger", ".log(", "log.info", "log.error",
    "console.log", "console.error", "console.warn", "winston", "pino", "bunyan",
    "metrics", "meter", "counter", "histogram", "gauge",
    "tracer", "span", "otel", "opentelemetry", "statsd", "prometheus",
    "slog", "zap", "logrus", "zerolog",
)


def has_instrumentation(blob: str) -> bool:
    """给定一段文本（会小写化后匹配），判断是否已埋点。跨语言共享。"""
    low = blob.lower()
    return any(h in low for h in INSTRUMENTATION_HINTS)
