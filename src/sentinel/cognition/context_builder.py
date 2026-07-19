"""上下文构建器（Context Builder）—— 判定前「拼一份有预算、有溯源的上下文」。

这是上下文工程（DESIGN §4/§9）的核心件。把散落的证据源统一成可插拔的
`EvidenceProvider`，每个源产出若干带**优先级 + 溯源**的 `ContextSection`；构建器按
优先级在 **token 预算**内贪心取舍，超预算的记为 dropped。产出的 `BuiltContext`：
  - `.text`     渲染好的上下文块，直接喂 LLM；
  - `.sections` 入选片段（含来源/引用/token），供界面展开「拼了啥、各占多少、丢了啥」。

内置 5 个证据源（优先级从高到低）：
  Target(函数本身，永不丢) > History(该函数历史反馈=强先验) > Note(相关笔记)
  > Peer(RAG 召回的已埋点相似函数) > Knowledge(RED/USE 经验)。

设计原则：可插拔（加一个 provider 即多一路证据）、有预算（大仓不撑爆上下文）、
可溯源（每条证据能被 verdict 引用，接地防幻觉 §7.3）、容错（一个源挂了不影响整体）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sentinel.model.code_unit import CodeUnit

# 来源标识与渲染顺序（逻辑阅读顺序，与优先级解耦）。
SRC_TARGET = "target"
SRC_HISTORY = "history"
SRC_NOTE = "note"
SRC_PEER = "peer"
SRC_KNOWLEDGE = "knowledge"
SRC_FOCUS = "focus"              # 对话级：当前焦点仓库（指代消解，如「再扫一遍」）
SRC_SCAN = "scan"                # 对话级：上次扫描的盲区与语言缺口
SRC_RUNS = "runs"                # 对话级：跨会话的历史扫描记录（情节记忆）
SRC_CONVERSATION = "conversation"  # 对话级：近期对话
_RENDER_ORDER = [SRC_TARGET, SRC_FOCUS, SRC_SCAN, SRC_RUNS, SRC_HISTORY, SRC_NOTE, SRC_PEER,
                 SRC_KNOWLEDGE, SRC_CONVERSATION]

_SRC_HEADER = {
    SRC_TARGET: "[FUNCTION] 待判定函数 | function under judgement",
    SRC_HISTORY: "[HISTORY] 该函数的历史反馈 | prior feedback on this function",
    SRC_NOTE: "[NOTES] 相关团队笔记 | relevant team notes",
    SRC_PEER: "[PEERS] 同仓库已埋点的相似函数 | similar already-instrumented peers",
    SRC_KNOWLEDGE: "[KNOWLEDGE] RED/USE 经验 | RED/USE knowledge",
    SRC_FOCUS: "[FOCUS] 当前对话焦点 | current conversation focus",
    SRC_SCAN: "[LAST SCAN] 上次扫描结果 | blind spots and language gaps from last scan",
    SRC_RUNS: "[PAST SCANS] 历史扫描记录（跨会话）| past scans across sessions",
    SRC_CONVERSATION: "[CONVERSATION] 近期对话 | recent conversation",
}


def estimate_tokens(text: str) -> int:
    """粗估 token 数：约 4 字符 ≈ 1 token。跨语言的启发式，够做预算取舍。

    （中文实际 token/字更高，这里偏保守；预算是软约束，估偏一点不影响正确性。）
    """
    return max(1, len(text or "") // 4)


@dataclass
class ContextTarget:
    """一次上下文构建的目标。

    同一个 ContextBuilder 靠不同 provider 服务不同场景，所以这里把两类场景的字段都放一起：
    - 判定场景（judge）：unit / repo / signals。
    - 对话场景（每轮 plan/act）：goal / turns（近期对话）/ last_scan（上次扫描快照）。
    provider 各取所需，不相关的字段留空即可。
    """
    unit: Optional[CodeUnit] = None
    repo: str = ""
    signals: List[str] = field(default_factory=list)
    goal: str = ""                                   # 本轮用户目标（对话级）
    turns: List = field(default_factory=list)        # 近期对话 [(role, content), ...]
    last_scan: Optional[dict] = None                 # 上次扫描快照 {repo, spots:[{unit_id,signals}]}
    focus_repo: str = ""                             # 当前焦点仓库（「再扫一遍」等指代的确定目标；空则由 provider 消解）

    @property
    def unit_id(self) -> str:
        return self.unit.unit_id if self.unit is not None else ""


@dataclass
class ContextSection:
    """一条上下文片段：带来源、优先级、溯源引用与 token 估算。

    压缩支持：`compact` 是本段的**精简版**（provider 可选提供）；预算不够时构建器会
    先降级到 compact、再截断，最后才丢弃（graceful degradation，而非整段硬删）。
    `level` 记录最终落地的形态：full / compact / clipped / dropped。
    """
    source: str                 # target|history|note|peer|knowledge
    title: str                  # 人读小标题
    content: str                # 正文（会进 prompt）
    priority: int               # 越大越优先保留
    ref: str = ""               # 溯源引用：unit_id / note:<id> / knowledge:<signal>
    compact: str = ""           # 精简版正文（可选；空则截断 content 兜底）
    min_chars: int = 40         # 截断下限：低于此就不值得留，直接丢
    tokens: int = 0             # 估算 token（build 时填）
    included: bool = True       # 是否入选（未入选=被预算丢弃）
    level: str = "full"         # full | compact | clipped | dropped

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "ref": self.ref,
                "tokens": self.tokens, "included": self.included,
                "priority": self.priority, "level": self.level}


@dataclass
class BuiltContext:
    """构建结果：入选片段 + 被丢片段 + 渲染文本 + 预算账。"""
    sections: List[ContextSection]
    dropped: List[ContextSection]
    text: str
    total_tokens: int
    budget: int

    def refs(self) -> List[str]:
        return [s.ref for s in self.sections if s.ref]

    def to_trace(self) -> List[dict]:
        """给界面展开：每条片段拼了啥、占多少 token、是否入选。"""
        return [s.to_dict() for s in self.sections] + [s.to_dict() for s in self.dropped]


class EvidenceProvider:
    """证据源接口：吃目标，吐若干 ContextSection。子类实现 provide。"""

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        raise NotImplementedError


class ContextBuilder:
    """按优先级 + token 预算，把多路证据拼成一份上下文（含压缩）。

    压缩策略（确定性优先，无 LLM）：
      1. 去重：同来源同引用 / 完全相同内容的片段只留一条。
      2. 分级降级：超预算时**先降到 compact、再截断，最后才丢弃**（不整段硬删）。
      3. 可选 LLM 压缩钩子 `compressor(text, target_tokens) -> text`：给超长片段做摘要；
         默认 None（保持 air-gapped / 确定性），失败自动回退到截断。
    """

    def __init__(self, providers: List[EvidenceProvider], token_budget: int = 1200,
                 compressor=None):
        self.providers = providers
        self.token_budget = token_budget
        self.compressor = compressor  # 可选：callable(text, target_tokens)->text

    def build(self, target: ContextTarget) -> BuiltContext:
        sections: List[ContextSection] = []
        for p in self.providers:
            try:
                sections.extend(p.provide(target) or [])
            except Exception:  # noqa: BLE001  一个源失败不拖垮整体（容错 §13）
                continue
        for s in sections:
            s.tokens = estimate_tokens(s.content)

        # 稳定按优先级降序取舍（同优先级保持加入顺序）。
        order = {id(s): i for i, s in enumerate(sections)}
        sections.sort(key=lambda s: (-s.priority, order[id(s)]))
        sections = self._dedup(sections)

        kept: List[ContextSection] = []
        dropped: List[ContextSection] = []
        used = 0
        for i, s in enumerate(sections):
            remaining = self.token_budget - used
            fit = self._fit(s, remaining, mandatory=(i == 0))
            if fit is None:
                s.included = False
                s.level = "dropped"
                dropped.append(s)
                continue
            s.content, s.level, s.tokens = fit
            s.included = True
            used += s.tokens
            kept.append(s)

        text = self._render(kept)
        return BuiltContext(kept, dropped, text, used, self.token_budget)

    @staticmethod
    def _dedup(sections: List[ContextSection]) -> List[ContextSection]:
        """去重：同 (来源, 引用) 或完全相同正文的片段只留优先级最高的那条。"""
        seen_ref = set()
        seen_content = set()
        out: List[ContextSection] = []
        for s in sections:
            ref_key = (s.source, s.ref) if s.ref else None
            if ref_key and ref_key in seen_ref:
                continue
            if s.content in seen_content:
                continue
            if ref_key:
                seen_ref.add(ref_key)
            seen_content.add(s.content)
            out.append(s)
        return out

    def _fit(self, s: ContextSection, remaining: int, mandatory: bool):
        """把片段塞进剩余预算：full → compact → (可选 LLM 摘要) → 截断 → 放不下则 None。

        mandatory=True（待判定函数）：即使超预算也必须返回一个形态（宁可超也要留）。
        返回 (content, level, tokens) 或 None。
        """
        full_tok = estimate_tokens(s.content)
        if full_tok <= remaining:
            return s.content, "full", full_tok
        # 降级到精简版
        if s.compact:
            ctok = estimate_tokens(s.compact)
            if ctok <= remaining:
                return s.compact, "compact", ctok
        base = s.compact or s.content
        # 还要更小：优先用 LLM 摘要（信息量高于暴力截断），失败/无则截断。
        if self.compressor is not None and remaining > 0:
            try:
                summ = self.compressor(base, remaining)
                if summ:
                    if estimate_tokens(summ) > remaining:
                        summ = summ[: remaining * 4 - 1].rstrip() + "…"
                    return summ, "summarized", estimate_tokens(summ)
            except Exception:  # noqa: BLE001  压缩失败 → 回退到截断
                pass
        # 截断到剩余预算（约 remaining*4 字符），但不低于 min_chars 才值得留
        if remaining * 4 >= s.min_chars:
            clipped = base[: remaining * 4 - 1].rstrip() + "…"
            return clipped, "clipped", estimate_tokens(clipped)
        if mandatory:
            # 必留：放精简版或原文（可超预算，因为没有它无法判定）
            return (s.compact or s.content), ("compact" if s.compact else "full"), \
                   estimate_tokens(s.compact or s.content)
        return None

    def _render(self, kept: List[ContextSection]) -> str:
        """按逻辑分组顺序渲染入选片段（不是按优先级），便于模型阅读。"""
        by_src: Dict[str, List[ContextSection]] = {}
        for s in kept:
            by_src.setdefault(s.source, []).append(s)
        blocks: List[str] = []
        for src in _RENDER_ORDER:
            group = by_src.get(src)
            if not group:
                continue
            blocks.append(_SRC_HEADER.get(src, src) + ":")
            for s in group:
                blocks.append(s.content.rstrip())
            blocks.append("")
        return "\n".join(blocks).rstrip()


# =====================================================================
# 内置证据源
# =====================================================================

def _trunc(text: str, n: int = 160) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


class TargetProvider(EvidenceProvider):
    """函数本身：签名/docstring/calls/信号/是否已埋点。优先级最高，永不丢。"""

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        u = target.unit
        if u is None:
            return []
        content = (
            f"unit_id: {u.unit_id}\n"
            f"signature: {u.qualname}{u.signature}\n"
            f"docstring: {_trunc(u.docstring)}\n"
            f"calls: {', '.join(u.calls) or '(none)'}\n"
            f"dependency signals: {', '.join(target.signals) or '(none)'}\n"
            f"has_instrumentation: {u.has_instrumentation}"
        )
        # 精简版：只留判定最关键的三行（签名/调用/信号），丢 docstring 与已埋点行。
        compact = (
            f"unit_id: {u.unit_id}\n"
            f"signature: {u.qualname}{u.signature}\n"
            f"calls: {', '.join(u.calls) or '(none)'} | signals: {', '.join(target.signals) or '(none)'}"
        )
        return [ContextSection(SRC_TARGET, "target", content, priority=100,
                               ref=u.unit_id, compact=compact)]


class HistoryProvider(EvidenceProvider):
    """该函数的历史反馈（用户此前标 ignore/instrument）= 强先验。"""

    def __init__(self, episodic):
        self.episodic = episodic

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        if self.episodic is None or not target.repo or not target.unit_id:
            return []
        rows = [r for r in self.episodic.list_feedback(target.repo)
                if r.unit_id == target.unit_id]
        if not rows:
            return []
        latest = rows[0]  # list_feedback 已按时间倒序
        zh = {"ignore": "不需要埋点", "instrument": "需要埋点"}.get(latest.decision, latest.decision)
        content = f"- 用户此前将该函数标记为「{zh}」({latest.decision})" + (
            f"，备注：{_trunc(latest.note)}" if latest.note else "")
        compact = f"- 历史裁决：{latest.decision}"
        return [ContextSection(SRC_HISTORY, "prior feedback", content, priority=92,
                               ref=f"feedback:{latest.decision}", compact=compact)]


class NoteProvider(EvidenceProvider):
    """相关团队笔记：按作用域(unit>repo>global)与标签(信号)重叠召回。"""

    def __init__(self, notes, max_notes: int = 4):
        self.notes = notes
        self.max_notes = max_notes

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        if self.notes is None:
            return []
        # 注意：不要因 repo 为空就 return——全局笔记（如「我是负责人 jiojio」这类身份/跨仓库约定）
        # 必须在任何对话里都能被召回（search_notes 对空 repo 会返回全局笔记）。
        hits = self.notes.search_notes(
            repo=target.repo, unit_id=target.unit_id,
            tags=target.signals, limit=self.max_notes,
        )
        out: List[ContextSection] = []
        for sn in hits:
            n = sn.note
            # 优先级按作用域：unit 95 / repo 78 / global 64。
            pri = {"unit": 95, "repo": 78, "global": 64}.get(n.scope, 64)
            tag = f" [{', '.join(n.tags)}]" if n.tags else ""
            content = f"- ({n.scope}{tag}) {_trunc(n.text, 200)}"
            compact = f"- ({n.scope}) {_trunc(n.text, 80)}"
            out.append(ContextSection(SRC_NOTE, f"note#{n.id}", content,
                                      priority=pri, ref=f"note:{n.id}", compact=compact))
        return out


class PeerProvider(EvidenceProvider):
    """RAG 召回同仓库、已埋点、非自己的相似函数（照抄团队既有埋点习惯）。"""

    def __init__(self, index, max_peers: int = 3):
        self.index = index
        self.max_peers = max_peers

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        if self.index is None or target.unit is None:
            return []
        from sentinel.cognition.embedder import embedding_text
        q = embedding_text(target.unit)

        def _keep(p: Dict[str, Any]) -> bool:
            return bool(p.get("has_instrumentation")) and p.get("unit_id") != target.unit_id

        hits = self.index.retrieve(q, k=self.max_peers, predicate=_keep)
        out: List[ContextSection] = []
        for rank, h in enumerate(hits):
            p = h.payload
            content = (f"- {p.get('unit_id')} | calls: "
                       f"{', '.join(p.get('calls', [])) or '(none)'}")
            compact = f"- {p.get('unit_id')}"
            out.append(ContextSection(SRC_PEER, p.get("qualname", "peer"), content,
                                      priority=70 - rank, ref=str(p.get("unit_id", "")),
                                      compact=compact))
        return out


class KnowledgeProvider(EvidenceProvider):
    """信号 → RED/USE 推荐观测（确定性经验，可引用为 knowledge:<信号>）。"""

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        from sentinel.engines.knowledge import knowledge_for
        know = knowledge_for(target.signals)
        out: List[ContextSection] = []
        for sig, obs in know.items():
            content = f"- knowledge:{sig} → {', '.join(obs)}"
            out.append(ContextSection(SRC_KNOWLEDGE, sig, content, priority=55,
                                      ref=f"knowledge:{sig}", compact=f"- knowledge:{sig}"))
        return out


def default_judge_builder(index=None, notes=None, episodic=None,
                          token_budget: int = 1200) -> ContextBuilder:
    """judge_intent 用的默认构建器：Target + History + Note + Peer + Knowledge。"""
    return ContextBuilder(
        providers=[
            TargetProvider(),
            HistoryProvider(episodic),
            NoteProvider(notes),
            PeerProvider(index),
            KnowledgeProvider(),
        ],
        token_budget=token_budget,
    )


# =====================================================================
# 对话级证据源（每轮 plan/act 用同一套 ContextBuilder）
# =====================================================================

class FocusProvider(EvidenceProvider):
    """对话焦点消解：把「再扫一遍 / 把这个修掉 / 这个文件补一下」这类指代接到具体对象。

    分两层：①确定性锚点——从近期对话/目标里按已知仓库 basename 就近匹配出焦点仓库（省 token、稳）；
    ②语义引导——在片段里引导 LLM 结合 [LAST SCAN] 盲区与 [CONVERSATION] 近期对话，
    消解「这个/那个/这种/这个文件」等指代到具体对象（仓库/文件/盲区函数/信号），
    定位不了就让 act 走 Finish 反问（配合 act 兑底，不臆造、不崩）。
    """

    def __init__(self, episodic=None):
        self.episodic = episodic

    def _known_repos(self) -> List[str]:
        if self.episodic is None:
            return []
        try:
            return list(dict.fromkeys(r.repo for r in self.episodic.list_runs(limit=50)))
        except Exception:  # noqa: BLE001 —— 一个证据源挂了不拖垮整体
            return []

    def _resolve(self, target: ContextTarget) -> str:
        if (target.focus_repo or "").strip():
            return target.focus_repo.strip()
        # 从近期对话（近→远）+ 目标里，按已知仓库 basename 就近匹配。
        repos = self._known_repos()
        texts = [c for _, c in reversed(list(target.turns or []))] + [target.goal or ""]
        for t in texts:
            low = (t or "").lower()
            for repo in repos:
                name = os.path.basename(repo.rstrip("/")).lower()
                if name and name in low:
                    return repo
        # 回退：本会话上次扫描。
        return ((target.last_scan or {}).get("repo") or target.repo or "").strip()

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        focus = self._resolve(target)
        if focus:
            content = (
                f"当前对话焦点仓库 | focus repo: {focus}\n"
                "指代消解：用户说「这个 / 那个 / 这种 / 把它修掉 / 上面那个 / 这个文件」等指代时，"
                "结合 [LAST SCAN] 的盲区列表与 [CONVERSATION] 近期对话，定位具体对象"
                "（仓库 / 某个文件 / 某个盲区函数 / 某类信号）；能定位就据此操作，"
                "调工具用绝对路径；定位不了或没有对应工具，就用 Action: Finish[...] 反问说明，切勿臆造。")
            compact = f"焦点 | focus: {focus}"
            return [ContextSection(SRC_FOCUS, "focus", content, priority=96,
                                   ref="", compact=compact, min_chars=20)]
        content = (
            "未能确定对话焦点对象。遇到「这个 / 那个 / 这种 / 把它补一下」等指代，"
            "请结合 [LAST SCAN] 盲区列表与 [CONVERSATION] 近期对话消解；"
            "实在定位不了或缺少可用工具，用 Action: Finish[...] 说明并反问，切勿臆造路径或对象。")
        return [ContextSection(SRC_FOCUS, "focus", content, priority=96,
                               ref="", compact=content[:40], min_chars=20)]


class LastScanProvider(EvidenceProvider):
    """上次扫描结果：盲区与语言缺口都必须成为下一轮可引用的上下文证据。"""

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        ls = target.last_scan
        if not ls:
            return []
        spots = ls.get("spots") or []
        gap = ls.get("language_gap") or {}
        if not spots and not gap:
            return []

        lines = [f"上次扫描仓库 | repo: {ls.get('repo', '')}"]
        compact_parts = []
        if spots:
            lines.append("盲区（用户说「忽略/这个不用/不用加」通常指这些，请对相应 unit_id 调 ignore_finding）:")
            for s in spots[:10]:
                sig = ", ".join(s.get("signals", [])) or "-"
                lines.append(f"  - {s['unit_id']} [{sig}]")
            compact_parts.append("盲区: " + ", ".join(s["unit_id"] for s in spots[:10]))
        if gap:
            languages = "、".join(f"{language}({count} 文件)" for language, count in gap.items())
            lines.extend([
                f"语言缺口：{languages} 因缺少解析器未被扫描。",
                "若用户表达同意补齐/安装这些语言支持，应调用 install_language_support（逐个语言）；"
                "该操作可能安装依赖，只有明确同意时才执行。成功后可对上述 repo 再调用 scan。",
            ])
            compact_parts.append(f"语言缺口: {languages}；获同意后 install_language_support，再 scan")
        full = "\n".join(lines)
        compact = "上次扫描 | " + "；".join(compact_parts)
        return [ContextSection(SRC_SCAN, "last scan", full, priority=88,
                               ref="", compact=compact, min_chars=20)]


class EpisodicRunsProvider(EvidenceProvider):
    """历史扫描记录（跨会话）：让「我们扫过什么仓库、进度如何」这类回忆有据可依。

    读情节记忆（episodic.db 的 runs 表），按仓库聚合：扫了几次 / 最近一次时间 / 最近盲区数。
    补上“记忆的另一半”：LastScanProvider 只看本会话内存，重启即失忆；这里从持久化库读。
    """

    def __init__(self, episodic, max_repos: int = 6):
        self.episodic = episodic
        self.max_repos = max_repos

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        if self.episodic is None:
            return []
        try:
            runs = self.episodic.list_runs(limit=50)
        except Exception:  # noqa: BLE001 —— 一个证据源挂了不拖垮整体
            return []
        if not runs:
            return []
        # runs 已按 id DESC（新→旧）；按仓库聚合，首次遇到即最新一次。
        agg: Dict[str, dict] = {}
        for r in runs:
            a = agg.get(r.repo)
            if a is None:
                agg[r.repo] = {"count": 1, "last_ts": r.ts,
                               "last_blind": r.blind_spot_count}
            else:
                a["count"] += 1
        items = list(agg.items())[:self.max_repos]
        lines = ["历史扫描记录（跨会话，来自情节记忆）| past scans:"]
        for repo, a in items:
            name = os.path.basename(repo.rstrip("/")) or repo
            lines.append(
                f"  - {name}：共 {a['count']} 次，最近 {a['last_ts'][:10]}，"
                f"最近一次 {a['last_blind']} 个盲区（{repo}）")
        full = "\n".join(lines)
        compact = "历史扫描 | past scans: " + ", ".join(
            (os.path.basename(repo.rstrip("/")) or repo) for repo, _ in items)
        return [ContextSection(SRC_RUNS, "past scans", full, priority=76,
                               ref="", compact=compact, min_chars=20)]


class ConversationProvider(EvidenceProvider):
    """近期对话：让多轮指代能解析。越近的轮次优先级越高。"""

    def __init__(self, max_turns: int = 4):
        self.max_turns = max_turns

    def provide(self, target: ContextTarget) -> List[ContextSection]:
        turns = list(target.turns or [])[-self.max_turns:]
        out: List[ContextSection] = []
        for i, item in enumerate(turns):
            try:
                role, content = item
            except (ValueError, TypeError):
                continue
            who = "用户" if role == "user" else "Sentinel"
            full = f"- {who}: {(content or '').strip()}"
            compact = f"- {who}: {_trunc(content, 80)}"
            out.append(ContextSection(SRC_CONVERSATION, f"turn{i}", full,
                                      priority=58 + i,  # 越近越高
                                      ref="", compact=compact, min_chars=16))
        return out


def default_turn_builder(notes=None, episodic=None, token_budget: int = 800) -> ContextBuilder:
    """每轮 plan/act 用的默认构建器：Focus + LastScan + PastScans + Note（仓库约定）+ Conversation。

    与 judge 用同一套 ContextBuilder（预算/去重/压缩/溯源一致），只是换了 provider 组合。
    FocusRepoProvider 把「再扫一遍」等指代确定到具体仓库；episodic 接入跨会话历史扫描记忆。
    """
    return ContextBuilder(
        providers=[
            FocusProvider(episodic),
            LastScanProvider(),
            EpisodicRunsProvider(episodic),
            NoteProvider(notes),
            ConversationProvider(),
        ],
        token_budget=token_budget,
    )

