"""意图判定 judge_intent（第 4 步·第 9 章上下文工程 + 语义判定）。

两级漏斗的第二级：scan 粗筛出盲区候选后，本模块给每个候选**拼一份有预算、有溯源的
上下文**（交给 cognition.context_builder），再让 LLM 判定「该不该埋点、该打什么点」。

上下文由可插拔证据源组装（优先级从高到低）：
  Target(函数本身) > History(该函数历史反馈) > Note(相关团队笔记)
  > Peer(RAG 召回的已埋点相似函数) > Knowledge(RED/USE 经验)。
构建器在 token 预算内取舍，超预算的丢弃并记录——判定用的上下文因此可控、可解释。

接地（grounding · §7.3）：只依据上下文下结论、引用证据、低置信标「存疑」、无 LLM 则
降级为基于静态信号的通用建议（air-gapped 仍可用）。Prompt 为中英双语（§15）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sentinel.model.code_unit import CodeUnit
from sentinel.engines.scan import signals_of
from sentinel.engines.knowledge import knowledge_for
from sentinel.cognition.context_builder import (
    ContextBuilder, ContextTarget, default_judge_builder,
)

# 低于此置信度 → 标「存疑」，不自动进修复清单（§7.3，与用户确认阈值 0.5）。
_UNCERTAIN_BELOW = 0.5
_LOGGER = logging.getLogger(__name__)


# -- Prompt（中英双语双版本 · 已与用户共审 · DESIGN §15）------------------------

_JUDGE_SYSTEM = (
    # EN
    "You are Sentinel's observability judge. You are given a CONTEXT assembled from several "
    "sources: [FUNCTION] the function under judgement; [HISTORY] the user's prior feedback on "
    "it; [NOTES] relevant team notes/conventions; [PEERS] similar functions in the SAME repo "
    "that are already instrumented; [KNOWLEDGE] RED/USE guidance for its dependency signals. "
    "Decide whether this function should be instrumented, and if so, what to log / measure / "
    "trace.\n"
    "Rules: base your decision ONLY on the given context; PRIORITISE team NOTES and prior "
    "HISTORY when present; cite every piece of evidence you use by its ref (a peer unit_id, "
    "note:<id>, knowledge:<signal>, or feedback:<decision>); if evidence is weak or absent, "
    "LOWER the confidence. Output ONLY the JSON below, no prose.\n\n"
    # ZH
    "你是 Sentinel 的可观测性评审。你会收到一份「上下文」，它由多路证据拼成："
    "[FUNCTION] 待判定的函数；[HISTORY] 用户对它的历史反馈；[NOTES] 相关团队笔记/约定；"
    "[PEERS] 同一仓库里已埋点的相似函数；[KNOWLEDGE] 其依赖信号的 RED/USE 经验。"
    "据此判断该函数是否应该埋点，若应该，则给出该打什么日志/指标/追踪。\n"
    "规则：只依据给定上下文下结论；有团队 NOTES 或历史 HISTORY 时**优先遵从**；"
    "引用你用到的每条证据的 ref（相似函数 unit_id、note:<编号>、knowledge:<信号>、"
    "或 feedback:<裁决>）；证据薄弱或缺失时调低置信度。只输出下面的 JSON，不要额外文字。\n\n"
    # Schema（字面 JSON，本字符串不经过 .format，花括号无需转义）
    "JSON schema:\n"
    '{\n'
    '  "verdict": "instrument | skip",\n'
    '  "confidence": 0.0,            // 0~1\n'
    '  "suggestions": [ {"type": "log|metric|trace", "what": "..."} ],\n'
    '  "evidence": ["file::peer_qualname", "note:3", "knowledge:http"],\n'
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
    context: List[Dict] = field(default_factory=list)   # 拼进上下文的片段溯源（供界面展开）

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id, "verdict": self.verdict, "confidence": self.confidence,
            "suggestions": self.suggestions, "evidence": self.evidence,
            "reason": self.reason, "status": self.status, "context": self.context,
        }


# -- 主判定 ----------------------------------------------------------------

def judge_intent(unit: CodeUnit, index, llm, *, repo: str = "",
                 notes=None, episodic=None,
                 builder: Optional[ContextBuilder] = None,
                 token_budget: int = 1200) -> Verdict:
    """判定单个函数该不该埋点、该打什么点。只读、不改任何东西。

    上下文由 ContextBuilder 组装（可传入自定义 builder；否则用默认五源构建器）。
    repo/notes/episodic 用于把「团队笔记」「历史反馈」也纳入上下文。
    """
    signals = signals_of(unit)
    if builder is None:
        builder = default_judge_builder(index=index, notes=notes, episodic=episodic,
                                        token_budget=token_budget)
    ctx = builder.build(ContextTarget(unit=unit, repo=repo or "", signals=signals))

    # 无 LLM：降级为「基于静态信号的通用建议」（air-gapped 仍可用 · §13.5）。
    if not getattr(llm, "available", False):
        return _generic_verdict(unit, signals, ctx.to_trace(), "LLM 不可用 | LLM unavailable")

    user = ("Decide based ONLY on the CONTEXT below. | 只依据下面的上下文判定。\n\n"
            "CONTEXT | 上下文:\n" + ctx.text)
    try:
        raw = llm.complete(_JUDGE_SYSTEM, user)
    except Exception:  # noqa: BLE001 - 外部 LLM 失败不能中断基础扫描报告。
        _LOGGER.exception("judge LLM call failed for %s", unit.unit_id)
        return _generic_verdict(unit, signals, ctx.to_trace(), "LLM 调用失败 | LLM call failed")
    verdict = _parse(raw, unit)
    verdict.context = ctx.to_trace()
    return verdict


def _generic_verdict(unit: CodeUnit, signals: List[str], context: List[Dict], reason: str) -> Verdict:
    """LLM 不可用时，用确定性知识库生成可展示的保底判定。"""
    knowledge = knowledge_for(signals)
    suggestions = [
        {"type": "metric", "what": observation}
        for observations in knowledge.values()
        for observation in observations
    ][:6]
    return Verdict(
        unit_id=unit.unit_id,
        verdict="instrument" if signals else "skip",
        confidence=0.0,
        suggestions=suggestions,
        evidence=[f"knowledge:{signal}" for signal in signals],
        reason=f"{reason}：基于静态信号给出的通用建议 | generic advice from signals",
        status="llm_unavailable",
        context=context,
    )


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
