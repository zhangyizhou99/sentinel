"""git 增量扫描（Drift · roadmap 第 6 步）。

只扫「相对 base 分支改动过的函数」的监控盲区——你这条 branch 从 main 分叉后
新写/改过的代码里，哪些碰了外部依赖却没埋点。

- 基准：默认 main（不存在退 master / origin/main），取它与 HEAD 的 merge-base（= 分叉点）。
- 范围：`git diff <分叉点>` 天然含 已提交到分支 + 暂存 + 未暂存；未跟踪的新文件另行纳入。
- 粒度：解析 diff 的改动行号，与函数行区间求交集 → 只报你真正动到的函数（不是整文件全报）。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

from sentinel.engines.scan import ScanResult, scan_file
from sentinel.scanners import get_scanner_for


class GitScanError(RuntimeError):
    """git 不可用 / 不是仓库 / 找不到基准分支时抛出。"""


def _git(repo: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=check, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
    except FileNotFoundError as exc:
        raise GitScanError("未找到 git | git not found") from exc
    except subprocess.CalledProcessError as exc:
        raise GitScanError(f"git 失败 | git failed: {exc.stderr.strip()}") from exc


def _is_git_repo(repo: str) -> bool:
    r = _git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _ref_exists(repo: str, ref: str) -> bool:
    return _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0


def resolve_base(repo: str, base: Optional[str] = None) -> str:
    """确定比较基点：默认 main（退 master/origin），取它与 HEAD 的 merge-base（分叉点）。"""
    candidates = [base] if base else ["main", "master", "origin/main", "origin/master"]
    ref = next((c for c in candidates if c and _ref_exists(repo, c)), None)
    if ref is None:
        raise GitScanError(
            f"找不到基准分支：{base or 'main/master'}；可用 --base 指定 | base branch not found")
    mb = _git(repo, ["merge-base", ref, "HEAD"], check=False)
    if mb.returncode == 0 and mb.stdout.strip():
        return mb.stdout.strip()
    return ref  # 无共同祖先（如全新仓库）就直接跟该 ref 比


# 解析 unified diff 的 hunk 头：@@ -旧 +新起始,新行数 @@
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_line_ranges(repo: str, base: str) -> Dict[str, Set[int]]:
    """每个文件「新侧」被改动的行号集合。

    `git diff --unified=0 <base>`（不带第二个 ref）= base 与**工作区**的差异，
    天然包含 已提交 + 暂存 + 未暂存（未跟踪新文件不在此，单独处理）。
    """
    out = _git(repo, ["diff", "--unified=0", "--no-color", "--no-renames", base]).stdout
    changed: Dict[str, Set[int]] = {}
    current: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip().strip('"')
            current = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
        elif line.startswith("@@") and current is not None:
            m = _HUNK.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                continue  # 纯删除，无新增行
            changed.setdefault(current, set()).update(range(start, start + count))
    return changed


def untracked_files(repo: str) -> List[str]:
    """未跟踪的新文件（排除 .gitignore 忽略的）。"""
    out = _git(repo, ["ls-files", "--others", "--exclude-standard"]).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _unit_touched(unit, lines: Set[int]) -> bool:
    return any(unit.start_line <= ln <= unit.end_line for ln in lines)


def scan_changed_repo(repo_path: str, base: Optional[str] = None) -> Tuple[ScanResult, str]:
    """只扫改动到的函数，返回 (ScanResult, base_分叉点)。"""
    repo = os.path.abspath(repo_path)
    if not _is_git_repo(repo):
        raise GitScanError(f"不是 git 仓库 | not a git repo: {repo}")
    base_ref = resolve_base(repo, base)
    ranges = changed_line_ranges(repo, base_ref)

    result = ScanResult(repo=repo)
    for rel, lines in ranges.items():
        if get_scanner_for(rel) is None:
            continue
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):      # 已删除的文件跳过
            continue
        for unit in scan_file(full, rel):
            if _unit_touched(unit, lines):
                result.units.append(unit)
    # 未跟踪的新文件：整文件都算「改动」，全部函数纳入
    for rel in untracked_files(repo):
        if get_scanner_for(rel) is None:
            continue
        full = os.path.join(repo, rel)
        if os.path.isfile(full):
            result.units.extend(scan_file(full, rel))
    return result, base_ref
