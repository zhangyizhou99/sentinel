"""意图判定 judge_intent（第 4 步·第 9 章上下文工程 + 语义判定）。

两级漏斗的第二级：scan 粗筛出盲区候选后，本模块给每个候选**拼一份证据**再让 LLM 判定
「该不该埋点、该打什么点」，而不是把函数裸奔丢给模型。

证据三来源（DESIGN §7.1，已与用户共审）：
  1. 函数本身（签名/docstring/calls/命中的信号）。
  2. RAG 召回的**同仓库、已埋点**的相似函数（照抄团队习惯，不凭空造）。
  3. RED/USE 知识（信号 → 推荐观测，见 knowledge.py）。

接地（grounding · §7.3）：只依据证据下结论、引用证据、低置信标「存疑」、无 LLM 则降级为
基于静态信号的通用建议（air-gapped 仍可用）。

Prompt 为中英双语双版本（§15），已与用户共同确认。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sentinel.model.code_unit import CodeUnit
from sentinel.engines.scan import signals_of
from sentinel.engines.knowledge import knowledge_for
from sentinel.cognition.embedder import embedding_text
from sentinel.cognition.index import CodeIndex

# 低于此置信度 → 标「存疑」，不自动进修复清单（§7.3，与用户确认阈值 0.5）。
_UNCERTAIN_BELOW = 0.5
# 证据的 token 预算：最多召回几个相似函数、docstring 截断长度。
_MAX_PEERS = 3
_DOC_TRUNC = 120


# -- Prompt（中英双语双版本 · 已与用户共审 · DESIGN §15）------------------------

_JUDGE_SYSTEM = (
    # EN
    "You are Sentinel's observability judge. Given a FUNCTION and EVIDENCE (how similar "
    "functions in the same repo were instrumented, plus RED/USE knowledge for its dependency "
    "signals), decide whether this function should be instrumented, and if so, what to "
    "log / measure / trace.\n"
    "Rules: base your decision ONLY on the given evidence; cite every piece of evidence you use "
    "(a peer unit_id, or knowledge:<signal>); if evidence is weak or absent, LOWER the confidence. "
    "Output ONLY the JSON below, no prose.\n\n"
    # ZH
    "你是 Sentinel 的可观测性评审。给定一个「函数」和「证据」（同仓库里相似函数是怎么埋点的，"
    "以及它依赖信号的 RED/USE 知识），判断这个函数是否应该埋点，若应该，则给出该打什么"
    "日志/指标/追踪。\n"
    "规则：只依据给定证据下结论；引用你用到的每一条证据（相似函数的 unit_id，或 "
    "knowledge:<信号>）；证据薄弱或缺失时调低置信度。只输出下面的 JSON，不要额外文字。\n\n"
    # Schema（字面 JSON，本字符串不经过 .format，花括号无需转义）
    "JSON schema:\n"
    '{\n'
    '  "verdict": "instrument | skip",\n'
    '  "confidence": 0.0,            // 0~1\n'
    '  "suggestions": [ {"type": "log|metric|trace", "what": "..."} ],\n'
    '  "evidence": ["file::peer_qualname", "knowledge:http"],\n'
    '  "reason": "一句话理由 | one-sentence reason"\n'
    '}'
)


# -- 判定结果 --------------------------------------------------------------

@dataclass
class Verdict:
    """一次意图判定的结构化结论。"""
    unit_id: str
    verdict: str = "skip"                 # instrument | skip
    confidence: float = 0.0
    suggestions: List[Dict[str, str]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    reason: str = ""
    status: str = "ok"                    # ok | uncertain | llm_unavailable | parse_error

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id, "verdict": self.verdict, "confidence": self.confidence,
            "suggestions": self.suggestions, "evidence": self.evidence,
            "reason": self.reason, "status": self.status,
        }


# -- 证据拼装（上下文工程）--------------------------------------------------

def _trunc(text: str, n: int = _DOC_TRUNC) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _peers(unit: CodeUnit, index: Optional[CodeIndex]) -> List[Dict[str, Any]]:
    """召回同仓库、已埋点、且不是自己的相似函数（RAG 证据）。"""
    if index is None:
        return []
    query = embedding_text(unit)

    def _keep(p: Dict[str, Any]) -> bool:
        return bool(p.get("has_instrumentation")) and p.get("unit_id") != unit.unit_id

    hits = index.retrieve(query, k=_MAX_PEERS, predicate=_keep)
    return [h.payload for h in hits]


def _render_function(unit: CodeUnit, signals: List[str]) -> str:
    return (
        f"unit_id: {unit.unit_id}\n"
        f"signature: {unit.qualname}{unit.signature}\n"
        f"docstring: {_trunc(unit.docstring)}\n"
        f"calls: {', '.join(unit.calls) or '(none)'}\n"
        f"dependency signals: {', '.join(signals) or '(none)'}\n"
        f"has_instrumentation: {unit.has_instrumentation}"
    )


def _render_evidence(peers: List[Dict[str, Any]], knowledge: Dict[str, List[str]]) -> str:
    lines: List[str] = ["[A] Similar already-instrumented functions | 相似的已埋点函数:"]
    if peers:
        for p in peers:
            lines.append(
                f"- {p.get('unit_id')} | calls: {', '.join(p.get('calls', [])) or '(none)'}"
            )
    else:
        lines.append("- (none found | 未召回)")
    lines.append("")
    lines.append("[B] RED/USE knowledge for signals | 信号的 RED/USE 知识:")
    if knowledge:
        for sig, obs in knowledge.items():
            lines.append(f"- knowledge:{sig} → {', '.join(obs)}")
    else:
        lines.append("- (none | 无)")
    return "\n".join(lines)


# -- 主判定 ----------------------------------------------------------------

def judge_intent(unit: CodeUnit, index: Optional[CodeIndex], llm) -> Verdict:
    """判定单个函数该不该埋点、该打什么点。只读、不改任何东西。"""
    signals = signals_of(unit)
    knowledge = knowledge_for(signals)

    # 无 LLM：降级为「基于静态信号的通用建议」（air-gapped 仍可用 · §13.5）。
    if not getattr(llm, "available", False):
        sugg = [{"type": "metric", "what": o} for obs in knowledge.values() for o in obs][:6]
        return Verdict(
            unit_id=unit.unit_id,
            verdict="instrument" if signals else "skip",
            confidence=0.0,
            suggestions=sugg,
            evidence=[f"knowledge:{s}" for s in signals],
            reason="LLM 不可用：基于静态信号给出的通用建议 | no LLM, generic advice from signals",
            status="llm_unavailable",
        )

    peers = _peers(unit, index)
    user = (
        "FUNCTION | 函数:\n" + _render_function(unit, signals) + "\n\n"
        "EVIDENCE | 证据:\n" + _render_evidence(peers, knowledge)
    )
    raw = llm.complete(_JUDGE_SYSTEM, user)
    return _parse(raw, unit)


def _parse(raw: str, unit: CodeUnit) -> Verdict:
    """解析 LLM 的 JSON 判定；容忍围栏，解析失败标 parse_error。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError):
        return Verdict(unit_id=unit.unit_id, reason="LLM 输出无法解析 | unparseable",
                       status="parse_error")

    try:
        conf = float(data.get("confidence", 0.0))
    except (ValueError, TypeError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    verdict = str(data.get("verdict", "skip")).strip().lower()
    verdict = "instrument" if verdict == "instrument" else "skip"
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    status = "uncertain" if conf < _UNCERTAIN_BELOW else "ok"
    return Verdict(
        unit_id=unit.unit_id, verdict=verdict, confidence=conf,
        suggestions=suggestions, evidence=[str(e) for e in evidence],
        reason=str(data.get("reason", "")), status=status,
    )
