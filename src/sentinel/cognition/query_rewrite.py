"""受约束的 LLM 查询重写：补全短句，但不创造事实或绕过权限。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


_REWRITE_SYSTEM = """You rewrite a user's short request for a code-maintenance agent.
Return STRICT JSON only:
{"rewritten_goal":"...","candidate_actions":["..."],"repo":"...","languages":["..."],"reason":"..."}

You may only use repo, languages, and candidate actions listed in FACTS. Never invent paths,
languages, functions, permissions, or consent. Installing language support is destructive and
requires explicit user consent; describe it as a candidate action only.
"""

_ALLOWED_ACTIONS = {"find_repo", "check_language_support", "install_language_support", "scan",
                    "ignore_finding", "apply_instrumentation"}


@dataclass
class QueryRewriteTrace:
    raw_query: str
    facts: Dict[str, Any]
    draft: Dict[str, Any]
    llm_output: str = ""
    validated: Dict[str, Any] = field(default_factory=dict)
    validation_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _json_object(text: str) -> Dict[str, Any]:
    source = (text or "").strip()
    if source.startswith("```"):
        source = source.split("\n", 1)[-1] if "\n" in source else source
        source = source.rsplit("```", 1)[0].strip()
    start, end = source.find("{"), source.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(source[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _facts(goal: str, last_scan: dict | None) -> Dict[str, Any]:
    snapshot = last_scan or {}
    repo = str(snapshot.get("repo") or "")
    gap = snapshot.get("language_gap") or {}
    languages = sorted(str(language) for language in gap if language)
    actions = ["find_repo", "check_language_support", "scan"]
    if languages:
        actions.insert(2, "install_language_support")
    if snapshot.get("spots"):
        actions.extend(["ignore_finding", "apply_instrumentation"])
    return {
        "raw_query": goal,
        "repo": repo,
        "language_gap": {language: gap[language] for language in languages},
        "allowed_candidate_actions": actions,
    }


def _draft(facts: Dict[str, Any]) -> Dict[str, Any]:
    languages = list(facts["language_gap"])
    actions = ["scan"] if facts["repo"] else ["find_repo"]
    if languages:
        actions = ["install_language_support", "scan"]
    return {
        "repo": facts["repo"],
        "languages": languages,
        "candidate_actions": actions,
    }


def _validate(candidate: Dict[str, Any], facts: Dict[str, Any], draft: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    notes: List[str] = []
    repo = str(candidate.get("repo") or "")
    if repo != facts["repo"]:
        if repo:
            notes.append("已移除 LLM 生成的非事实仓库路径。")
        repo = facts["repo"]

    allowed_languages = set(facts["language_gap"])
    requested_languages = candidate.get("languages") or []
    if not isinstance(requested_languages, list):
        requested_languages = []
    languages = [str(language) for language in requested_languages if str(language) in allowed_languages]
    removed_languages = set(map(str, requested_languages)) - set(languages)
    if removed_languages:
        notes.append("已移除不在语言缺口中的语言：" + ", ".join(sorted(removed_languages)))
    if not languages and draft["languages"]:
        languages = list(draft["languages"])

    allowed_actions = set(facts["allowed_candidate_actions"]) & _ALLOWED_ACTIONS
    requested_actions = candidate.get("candidate_actions") or []
    if not isinstance(requested_actions, list):
        requested_actions = []
    actions = [str(action) for action in requested_actions if str(action) in allowed_actions]
    removed_actions = set(map(str, requested_actions)) - set(actions)
    if removed_actions:
        notes.append("已移除不允许的候选动作：" + ", ".join(sorted(removed_actions)))
    if not actions:
        actions = list(draft["candidate_actions"])

    rewritten_goal = str(candidate.get("rewritten_goal") or "").strip()
    if not rewritten_goal:
        rewritten_goal = facts["raw_query"]
    return {
        "rewritten_goal": rewritten_goal,
        "repo": repo,
        "languages": languages,
        "candidate_actions": actions,
        "reason": str(candidate.get("reason") or ""),
    }, notes


def rewrite_query(llm, raw_query: str, last_scan: dict | None = None) -> QueryRewriteTrace:
    """生成、校验查询重写结果；LLM 不可用或解析失败时退回确定性草案。"""
    facts = _facts(raw_query, last_scan)
    draft = _draft(facts)
    trace = QueryRewriteTrace(raw_query=raw_query, facts=facts, draft=draft)
    candidate: Dict[str, Any] = {}
    if getattr(llm, "available", False):
        prompt = "FACTS:\n" + json.dumps(facts, ensure_ascii=False, indent=2)
        try:
            trace.llm_output = llm.complete(_REWRITE_SYSTEM, prompt)
            candidate = _json_object(trace.llm_output)
        except Exception as exc:  # noqa: BLE001
            trace.validation_notes.append(f"LLM rewrite 失败，已回退草案：{type(exc).__name__}")
    else:
        trace.validation_notes.append("LLM 不可用，使用确定性草案。")
    trace.validated, notes = _validate(candidate, facts, draft)
    trace.validation_notes.extend(notes)
    return trace


def render_rewrite_context(trace: QueryRewriteTrace) -> str:
    """把已验证的 rewrite 作为约束性上下文，而不是可直接执行的命令。"""
    result = trace.validated
    lines = ["[QUERY REWRITE] 已验证的短查询补全：",
             f"原始输入: {trace.raw_query}",
             f"重写目标: {result.get('rewritten_goal', trace.raw_query)}"]
    if result.get("repo"):
        lines.append(f"事实仓库: {result['repo']}")
    if result.get("languages"):
        lines.append("事实语言缺口: " + ", ".join(result["languages"]))
    lines.append("候选动作: " + ", ".join(result.get("candidate_actions", [])))
    lines.append("约束：候选动作不是授权；scan 仍需目录授权，install_language_support 仍需用户明确同意。")
    return "\n".join(lines)