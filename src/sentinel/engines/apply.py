"""补埋点应用引擎（Restore）—— 见 DESIGN §8.3。

把 scan 出的盲区函数变成对源码的**真实修改**，落在一个用户命名的新 git 分支上，
并**保持未提交**（留成工作区改动，让人在编辑器里看/审/改，再自己决定提交或丢弃）。
原分支不受影响：不满意就 `git checkout <base>` 再删分支，毫无损失。

git 安全机制沿用 legacy `apply.py`（成熟）：是 git 仓库 / 工作区干净 / 分支不存在 三前置检查，
新分支 + 未提交 + `git add -N` 让新文件也进 diff。改写用函数级 `instrument_editor`（AST 安全网 + 幂等）。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sentinel.engines.instrument_editor import insert_instrumentation


@dataclass
class ApplyResult:
    branch: str
    base_branch: str
    units_fixed: List[str] = field(default_factory=list)   # 成功补埋点的 unit_id
    files_changed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)       # 跳过的 unit_id（非 py / 改写不安全）
    diff: str = ""
    message: str = ""


class ApplyError(RuntimeError):
    """前置条件失败时抛出。"""


def _build_snippet(unit, convention=None, procedural=None):
    """为盲区函数生成 (埋点行, import, 模板)。

    优先级：① 程序性记忆里学到的修复技能模板；② 项目埋点约定风格（入乡随俗，
    structlog/loguru/logging 各自的写法）；③ 复杂风格（OTel/metrics 一行补不了）退安全 logging。
    模板一律自包含（不依赖模块级 logger 变量），保证插入后一定能跑。
    """
    from sentinel.engines.scan import signals_of
    from sentinel.engines.conventions import snippet_for_style
    sigs = signals_of(unit)
    sig = "/".join(sigs) or "?"
    primary = sigs[0] if sigs else "?"
    lang = getattr(unit, "language", "") or "python"
    if procedural is not None:
        skill = procedural.get_skill(lang, primary)
        if skill:
            return (skill.snippet_template.format(qualname=unit.qualname, signal=sig),
                    skill.import_stmt, skill.snippet_template)
    style = convention.style if (convention and getattr(convention, "found", False)) else "logging"
    import_stmt, template = snippet_for_style(style)
    return template.format(qualname=unit.qualname, signal=sig), import_stmt, template


class Applier:
    """把盲区函数补埋点提交到新建 git 分支（未提交，待人审）。"""

    def apply(self, repo, units, branch: str, convention=None, procedural=None) -> ApplyResult:
        repo = Path(repo).resolve()
        branch = (branch or "").strip()
        if not branch:
            raise ApplyError("需要分支名（请自己输入）| branch name required")
        if not units:
            raise ApplyError("无盲区可补 | no blind spots to fix")

        self._require_git_repo(repo)
        self._require_clean(repo)
        base = self._current_branch(repo)
        self._require_new_branch(repo, branch)

        result = ApplyResult(branch=branch, base_branch=base)
        self._git(repo, ["checkout", "-b", branch])
        self._write_edits(repo, units, convention, procedural, result)
        self._git(repo, ["add", "-N", "."])
        result.diff = self._git(repo, ["diff"]).stdout
        result.message = (
            f"已切换到新分支 '{branch}'，{len(result.files_changed)} 个文件补了埋点，"
            f"**未提交**——去编辑器里看/改，再自行提交或丢弃（原分支 '{base}'）。"
        )
        return result

    def _write_edits(self, repo: Path, units, convention, procedural, result: ApplyResult) -> None:
        by_file: dict = {}
        for u in units:
            by_file.setdefault(u.file, []).append(u)
        for rel, us in sorted(by_file.items()):
            path = repo / rel
            if not rel.endswith(".py") or not path.exists():
                result.skipped.extend(u.unit_id for u in us)     # 起步只支持 Python
                continue
            source = path.read_text(encoding="utf-8")
            changed = False
            for u in us:
                snippet, import_stmt, template = _build_snippet(u, convention, procedural)
                new_source = insert_instrumentation(source, u.qualname, snippet, import_stmt)
                if new_source is None:
                    result.skipped.append(u.unit_id)             # 改写不安全/找不到函数/已埋点
                    continue
                source = new_source
                changed = True
                result.units_fixed.append(u.unit_id)
                # 程序性记忆：记住这次成功的补法（按约定风格的模板，同类盲区下次可复用）。
                if procedural is not None:
                    from sentinel.engines.scan import signals_of
                    sigs = signals_of(u)
                    procedural.record_skill(
                        getattr(u, "language", "") or "python",
                        sigs[0] if sigs else "?",
                        template, import_stmt)
            if changed:
                path.write_text(source, encoding="utf-8")
                result.files_changed.append(rel)

    # -- git 前置检查（沿用 legacy）------------------------------------------

    def _require_git_repo(self, repo: Path) -> None:
        r = self._git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
        if r.returncode != 0 or r.stdout.strip() != "true":
            raise ApplyError(f"不是 git 仓库 | not a git repo: {repo}")

    def _require_clean(self, repo: Path) -> None:
        if self._git(repo, ["status", "--porcelain"]).stdout.strip():
            raise ApplyError("工作区不干净，请先提交或 stash | working tree not clean")

    def _require_new_branch(self, repo: Path, branch: str) -> None:
        r = self._git(repo, ["rev-parse", "--verify", branch], check=False)
        if r.returncode == 0:
            raise ApplyError(f"分支已存在 | branch already exists: {branch}（请换个名字）")

    def _current_branch(self, repo: Path) -> str:
        return self._git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def _git(self, repo: Path, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=check, capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError as exc:
            raise ApplyError("未找到 git | git not found") from exc
        except subprocess.CalledProcessError as exc:
            raise ApplyError(f"git 失败 | git failed: {exc.stderr.strip()}") from exc
