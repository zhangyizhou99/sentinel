"""补埋点应用引擎（Restore）—— 见 DESIGN §8.3。

把 scan 出的盲区函数变成对源码的**真实修改**，**直接写回工作区文件、保持未提交**
（不建分支、不切分支、不提交），让人在编辑器里看/审/改，再自己决定 commit 或丢弃。
支持增量：先补几个、再补几个，都是往当前文件继续改（不要求干净工作区）。

改写用函数级 `instrument_editor`（AST 安全网 + 幂等）。git 是可选的：是 git 仓库时
用 `git add -N` + `git diff` 生成预览；不满意用 `git checkout -- <文件>` 撤销；非 git 仓库也能改。
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from sentinel.engines.instrument_editor import (
    insert_instrumentation,
    insert_js_import,
    insert_js_instrumentation,
)
from sentinel.engines.telemetry_contract import discover_frontend_contract


_JS_LANGUAGES = {"javascript", "typescript", "tsx"}


@dataclass(frozen=True)
class _SnippetCandidate:
    snippet: str = ""
    import_stmt: str = ""
    template: str = ""
    emitter: str = ""
    receiver_configured: Optional[bool] = None
    delivery: str = "unverified"
    delivery_note: str = ""
    reason: str = ""


@dataclass
class ApplyResult:
    units_fixed: List[str] = field(default_factory=list)   # 成功补埋点的 unit_id
    files_changed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)       # 跳过的 unit_id（非 py / 改写不安全）
    skipped_reasons: Dict[str, str] = field(default_factory=dict)
    emitter: str = ""
    receiver_configured: Optional[bool] = None
    delivery: str = "unverified"
    delivery_note: str = ""
    reflection: Dict[str, object] = field(default_factory=dict)
    diff: str = ""
    message: str = ""


class ApplyError(RuntimeError):
    """前置条件失败时抛出。"""


def _nearest_package_root(repo: Path, target: Path) -> Optional[Path]:
    """返回包含 target 的最近 package.json 目录，不跨出仓库。"""
    current = target.parent.resolve()
    root = repo.resolve()
    while current == root or root in current.parents:
        if (current / "package.json").is_file():
            return current
        if current == root:
            break
        current = current.parent
    return None


def _package_has_faro(package_root: Path) -> bool:
    try:
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    return "@grafana/faro-web-sdk" in dependencies


def _receiver_is_configured(package_root: Path, helper_source: str) -> bool:
    """只判断仓库/当前进程可见配置；不把空的 .env.example 当成已配置。"""
    def is_receiver_url(value: str) -> bool:
        return value.strip().strip("'\"").lower().startswith(("http://", "https://"))

    if is_receiver_url(os.environ.get("VITE_GRAFANA_FARO_URL", "")):
        return True
    if re.search(r"\burl\s*:\s*['\"]https?://", helper_source):
        return True
    for env_path in package_root.glob(".env*"):
        if env_path.name.endswith(".example") or not env_path.is_file():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            match = re.match(r"\s*VITE_GRAFANA_FARO_URL\s*=\s*(.*?)\s*$", line)
            if match and is_receiver_url(match.group(1)):
                return True
    return False


def _module_path(from_file: Path, helper: Path) -> str:
    relative = os.path.relpath(helper.with_suffix(""), from_file.parent).replace("\\", "/")
    return relative if relative.startswith(".") else f"./{relative}"


def _event_name(unit) -> str:
    raw = f"{Path(unit.file).stem}.{unit.qualname}"
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", snake).strip("_.").lower()


def _faro_candidate(repo: Path, target: Path, source: str, unit, signal: str) -> _SnippetCandidate:
    package_root = _nearest_package_root(repo, target)
    if package_root is None:
        return _SnippetCandidate(reason="未找到包含该文件的 package.json，无法确认前端遥测能力")
    if not _package_has_faro(package_root):
        return _SnippetCandidate(
            reason="项目未声明 @grafana/faro-web-sdk，拒绝用 console.info 冒充 Grafana 埋点")
    contract = discover_frontend_contract(package_root)
    if contract is None or contract.emitter != "grafana-faro":
        return _SnippetCandidate(
            reason="未找到连接 Faro sender 的可兼容 Telemetry helper")
    helper = contract.helper
    if helper.resolve() == target.resolve():
        return _SnippetCandidate(reason="不会在遥测 helper 自身递归插入事件")

    helper_source = helper.read_text(encoding="utf-8")
    receiver_configured = _receiver_is_configured(package_root, helper_source)
    export_name = contract.export_name
    already_imported = bool(re.search(
        rf"import\s*\{{[^}}]*\b{re.escape(export_name)}\b[^}}]*\}}\s*from",
        source, re.DOTALL))
    import_stmt = "" if already_imported else (
        f"import {{ {export_name} }} from '{_module_path(target, helper)}'")
    event_name = _event_name(unit)
    snippet = contract.call(
        json.dumps(event_name), json.dumps(signal), "{ phase: 'start' }")
    if snippet is None:
        return _SnippetCandidate(reason="Telemetry helper 签名与事件参数不兼容")
    delivery = "configured_unverified" if receiver_configured else "pending_configuration"
    note = (
        "已检测到 Faro Receiver 配置，但尚未执行真实网络投递验证。"
        if receiver_configured else
        "未检测到 VITE_GRAFANA_FARO_URL；源码 emitter 已接入，但事件尚不会发送到 Grafana。"
    )
    return _SnippetCandidate(
        snippet=snippet,
        import_stmt=import_stmt,
        emitter=contract.emitter,
        receiver_configured=receiver_configured,
        delivery=delivery,
        delivery_note=note,
    )


def _build_snippet(repo: Path, target: Path, source: str, unit,
                   convention=None, procedural=None, llm=None) -> _SnippetCandidate:
    """为盲区函数生成一个绑定到已验证 emitter 的候选埋点。

    优先级：① 程序性记忆里学到的修复技能模板；② 项目埋点约定风格（入乡随俗，
    structlog/loguru/logging 各自的写法）；③ 复杂风格（OTel/metrics 一行补不了）退安全 logging。
    JS/TS 只允许调用项目中已接入官方 Grafana Faro SDK 的 helper；其它语言若没有已验证
    emitter 则拒绝自动改写，不能再用 console/println 冒充遥测。
    """
    from sentinel.engines.scan import signals_of
    from sentinel.engines.conventions import snippet_for_style
    sigs = signals_of(unit)
    sig = "/".join(sigs) or "?"
    primary = sigs[0] if sigs else "?"
    lang = getattr(unit, "language", "") or "python"
    if lang in _JS_LANGUAGES:
        return _faro_candidate(repo, target, source, unit, primary)
    if lang != "python":
        return _SnippetCandidate(
            reason=f"{lang} 扫描已支持，但项目没有 Sentinel 可验证的真实 telemetry emitter")
    if procedural is not None:
        skill = procedural.get_skill(lang, primary)
        if skill:
            return _SnippetCandidate(
                snippet=skill.snippet_template.format(qualname=unit.qualname, signal=sig),
                import_stmt=skill.import_stmt,
                template=skill.snippet_template,
                emitter="python-logging",
                delivery_note="源码日志已接入；是否由 Grafana/Loki 采集尚未验证。",
            )
    style = convention.style if (convention and getattr(convention, "found", False)) else "logging"
    import_stmt, template = snippet_for_style(style)
    emitter = style if style in {"structlog", "loguru", "logging"} else "python-logging"
    return _SnippetCandidate(
        snippet=template.format(qualname=unit.qualname, signal=sig),
        import_stmt=import_stmt,
        template=template,
        emitter=emitter,
        delivery_note="源码日志已接入；是否由 Grafana/Loki 采集尚未验证。",
    )


class Applier:
    """把盲区函数补埋点**直接写回工作区文件**（未提交、不建分支，待人审）。"""

    def __init__(self, llm=None):
        self.llm = llm

    def apply(self, repo, units, convention=None, procedural=None) -> ApplyResult:
        repo = Path(repo).resolve()
        if not units:
            raise ApplyError("无盲区可补 | no blind spots to fix")
        if not any(getattr(unit, "language", "") for unit in units):
            raise ApplyError("这些盲区缺少已验证的语言信息，无法安全自动改写。")

        is_git = self._is_git_repo(repo)
        before_ids = {unit.unit_id for unit in self._blind_spots(repo)}

        result = ApplyResult()
        planned = self._plan_edits(repo, units, convention, procedural, result)
        if not planned:
            reasons = "；".join(sorted(set(result.skipped_reasons.values())))
            detail = f" 原因：{reasons}" if reasons else ""
            raise ApplyError(f"没有生成任何通过语法验证的补丁，仓库未修改。{detail}")

        # 直接写回工作区文件：不建分支、不提交、不要求干净工作区。
        # 增量友好——第二次补是在上一批（未提交）改动之上继续改同一批文件。
        for path, source in planned.items():
            path.write_text(source, encoding="utf-8")
            result.files_changed.append(str(path.relative_to(repo)))
        result.reflection = self._reflect(repo, units, result, before_ids)
        if is_git:
            self._git(repo, ["add", "-N", "."], check=False)   # 让新文件也进 diff
            result.diff = self._git(repo, ["diff"], check=False).stdout
        result.message = (
            f"已直接修改 {len(result.files_changed)} 个文件补埋点（**未提交**，留成工作区改动）。"
            f"去编辑器里看/改，满意就自己 commit，不满意用 `git checkout -- <文件>` 撤销。 "
            f"Emitter: {result.emitter or 'unknown'}；delivery: {result.delivery}。"
        )
        return result

    @staticmethod
    def _blind_spots(repo: Path):
        from sentinel.engines.scan import scan_repo
        return scan_repo(str(repo)).blind_spots

    @classmethod
    def _reflect(cls, repo: Path, units, result: ApplyResult,
                 before_ids: set[str]) -> Dict[str, object]:
        """写盘后做最小验证：语法有效，且盲区只按本次选择发生变化。"""
        languages = {unit.file: getattr(unit, "language", "") or "python" for unit in units}
        syntax_errors = []
        for relative in result.files_changed:
            path = repo / relative
            source = path.read_text(encoding="utf-8")
            language = languages.get(relative, "")
            try:
                if path.suffix.lower() == ".py":
                    ast.parse(source)
                else:
                    from sentinel.scanners.treesitter_scanner import source_parses
                    if not language or not source_parses(language, source):
                        syntax_errors.append(relative)
            except (OSError, SyntaxError, UnicodeError):
                syntax_errors.append(relative)

        after_ids = {unit.unit_id for unit in cls._blind_spots(repo)}
        fixed_ids = set(result.units_fixed)
        selected_remaining = sorted(fixed_ids & after_ids)
        unselected_before = before_ids - fixed_ids
        unexpected_resolved = sorted(unselected_before - after_ids)
        new_blind_spots = sorted(after_ids - before_ids)
        expected_after = before_ids - fixed_ids
        passed = not syntax_errors and after_ids == expected_after
        return {
            "passed": passed,
            "syntax_passed": not syntax_errors,
            "syntax_errors": syntax_errors,
            "selected_resolved": sorted(fixed_ids - after_ids),
            "selected_remaining": selected_remaining,
            "unselected_preserved": sorted(unselected_before & after_ids),
            "unexpected_resolved": unexpected_resolved,
            "new_blind_spots": new_blind_spots,
            "before_count": len(before_ids),
            "after_count": len(after_ids),
        }

    @staticmethod
    def _record_delivery(result: ApplyResult, candidate: _SnippetCandidate) -> None:
        if not result.emitter:
            result.emitter = candidate.emitter
        elif result.emitter != candidate.emitter:
            result.emitter = "mixed"
        if candidate.receiver_configured is not None:
            if result.receiver_configured is None:
                result.receiver_configured = candidate.receiver_configured
            else:
                result.receiver_configured = (
                    result.receiver_configured and candidate.receiver_configured)
        priority = {"unverified": 1, "configured_unverified": 2,
                    "pending_configuration": 3}
        if priority.get(candidate.delivery, 1) > priority.get(result.delivery, 1):
            result.delivery = candidate.delivery
        if candidate.delivery_note and candidate.delivery_note not in result.delivery_note:
            result.delivery_note = " ".join(
                part for part in (result.delivery_note, candidate.delivery_note) if part)

    def _plan_edits(self, repo: Path, units, convention, procedural,
                    result: ApplyResult) -> dict[Path, str]:
        """在建分支前于内存中生成并验证所有改动，避免失败留下空分支。"""
        by_file: dict = {}
        planned: dict[Path, str] = {}
        for u in units:
            by_file.setdefault(u.file, []).append(u)
        for rel, us in sorted(by_file.items()):
            path = repo / rel
            suffix = path.suffix.lower()
            if not path.exists():
                result.skipped.extend(u.unit_id for u in us)
                continue
            source = path.read_text(encoding="utf-8")
            changed = False
            pending_imports = set()
            ordered_units = us if suffix == ".py" else sorted(
                us, key=lambda unit: unit.start_line, reverse=True)
            for u in ordered_units:
                candidate = _build_snippet(
                    repo, path, source, u, convention, procedural, self.llm)
                if not candidate.snippet:
                    result.skipped.append(u.unit_id)
                    if candidate.reason:
                        result.skipped_reasons[u.unit_id] = candidate.reason
                    continue
                if suffix == ".py":
                    new_source = insert_instrumentation(
                        source, u.qualname, candidate.snippet, candidate.import_stmt)
                else:
                    new_source = insert_js_instrumentation(
                        source, u.start_line, u.end_line, candidate.snippet)
                    if new_source is not None:
                        from sentinel.scanners.treesitter_scanner import source_parses
                        imports = pending_imports | ({candidate.import_stmt}
                                                     if candidate.import_stmt else set())
                        validated_source = new_source
                        for import_stmt in sorted(imports):
                            validated_source = insert_js_import(validated_source, import_stmt)
                        if not source_parses(u.language, validated_source):
                            new_source = None
                if new_source is None:
                    result.skipped.append(u.unit_id)             # 改写不安全/找不到函数/已埋点
                    result.skipped_reasons[u.unit_id] = "候选补丁未通过函数定位或语法验证"
                    continue
                source = new_source
                if candidate.import_stmt:
                    pending_imports.add(candidate.import_stmt)
                changed = True
                result.units_fixed.append(u.unit_id)
                self._record_delivery(result, candidate)
                # 程序性记忆：记住这次成功的补法（按约定风格的模板，同类盲区下次可复用）。
                if procedural is not None and candidate.template:
                    from sentinel.engines.scan import signals_of
                    sigs = signals_of(u)
                    procedural.record_skill(
                        getattr(u, "language", "") or "python",
                        sigs[0] if sigs else "?",
                        candidate.template, candidate.import_stmt)
            if changed:
                for import_stmt in sorted(pending_imports):
                    source = insert_js_import(source, import_stmt)
                planned[path] = source
        return planned

    # -- git 可选：仅用于生成 diff 预览（不建分支、不要求干净工作区）--------------

    def _is_git_repo(self, repo: Path) -> bool:
        try:
            r = self._git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
        except ApplyError:
            return False   # 未装 git 也能只改文件，只是没有 diff 预览
        return r.returncode == 0 and r.stdout.strip() == "true"

    def _git(self, repo: Path, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=check, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
        except FileNotFoundError as exc:
            raise ApplyError("未找到 git | git not found") from exc
        except subprocess.CalledProcessError as exc:
            raise ApplyError(f"git 失败 | git failed: {exc.stderr.strip()}") from exc
