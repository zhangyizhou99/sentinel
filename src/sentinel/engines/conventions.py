"""埋点约定学习器（语义记忆 · 入乡随俗）—— 见 DESIGN §8.1。

问题：补埋点若用 Sentinel 外来风格（硬编码 logger.info），与项目现有约定不符、无法直接合并。
做法：**从项目已埋点的函数里学它的写法**，归纳「本项目埋点约定」，存进语义记忆（NoteStore），
让 judge / apply 照这个风格补 —— 风格一致，能直接合并。

全确定性（频次统计），无 LLM：素材是 scan 已标 has_instrumentation=True 的函数，
它们 calls 里的埋点调用（log.info / structlog.get_logger / tracer.start_span …）就是风格样本。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import List

from sentinel.model.code_unit import CodeUnit
from sentinel.scanners.instrumentation import has_instrumentation

# NoteStore 里自动约定笔记的固定标注（供幂等更新与识别）。
CONVENTION_TAG = "instrumentation-convention"
CONVENTION_AUTHOR = "sentinel-auto"


@dataclass
class InstrumentationConvention:
    """一份从项目里学到的埋点约定。"""
    repo: str
    style: str                                   # logging / structlog / loguru / opentelemetry / metrics / custom / none
    top_calls: List[str] = field(default_factory=list)  # 最常见的埋点调用点号名
    sample_count: int = 0                        # 学自多少个已埋点函数

    @property
    def found(self) -> bool:
        return self.style != "none" and bool(self.top_calls)

    def summary(self) -> str:
        """人读、也直接进上下文的一句话约定。"""
        if not self.found:
            return ""
        calls = "、".join(self.top_calls)
        return (f"本项目埋点约定（Sentinel 自动学习自 {self.sample_count} 个已埋点函数）："
                f"风格 = {self.style}；常见写法 = {calls}。补埋点时请遵循此风格，勿引入其它日志库。")


def _infer_style(calls: List[str]) -> str:
    """从埋点调用点号名归纳风格。"""
    blob = " ".join(calls).lower()
    if "structlog" in blob:
        return "structlog"
    if "loguru" in blob or "logger.add" in blob:
        return "loguru"
    if any(k in blob for k in ("opentelemetry", "otel", "tracer", "span")):
        return "opentelemetry"
    if any(k in blob for k in ("metrics", "meter", "counter", "histogram", "prometheus", "statsd")):
        return "metrics"
    if any(k in blob for k in ("logger", "logging", "getlogger", "log.", ".log(", "slog", "zap", "logrus")):
        return "logging"
    return "custom"


def learn_convention(repo: str, units: List[CodeUnit]) -> InstrumentationConvention:
    """从一批 CodeUnit 学出项目埋点约定（只看已埋点的函数）。"""
    instrumented = [u for u in units if getattr(u, "has_instrumentation", False)]
    counter: Counter = Counter()
    for u in instrumented:
        lang = getattr(u, "language", "") or ""
        for call in u.calls:
            if has_instrumentation(call, lang):   # 该调用本身像埋点写法
                counter[call] += 1
    if not counter:
        return InstrumentationConvention(repo=repo, style="none",
                                         top_calls=[], sample_count=len(instrumented))
    top = [c for c, _ in counter.most_common(5)]
    return InstrumentationConvention(repo=repo, style=_infer_style(top),
                                     top_calls=top, sample_count=len(instrumented))


def store_convention(notes, conv: InstrumentationConvention) -> int:
    """把约定写进语义记忆（NoteStore，repo 级）。幂等：先删旧的自动约定再写新的。返回笔记 id（无则 0）。"""
    if not conv.found:
        return 0
    for n in notes.list_notes(repo=conv.repo, limit=200):
        if n.author == CONVENTION_AUTHOR and CONVENTION_TAG in n.tags and n.repo:
            notes.delete_note(n.id)
    return notes.add_note(text=conv.summary(), repo=conv.repo,
                          tags=["埋点约定", CONVENTION_TAG], author=CONVENTION_AUTHOR)


def learn_and_store(repo: str, units: List[CodeUnit], notes) -> InstrumentationConvention:
    """学 + 存一步到位（scan 时顺带调用）。"""
    conv = learn_convention(repo, units)
    store_convention(notes, conv)
    return conv
