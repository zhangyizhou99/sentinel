"""把领域能力包装成 Agent 可调用的工具。

连接三层：engines/agent 的 Tool 抽象 ←→ engines/scan 的扫描能力 ←→ permissions 的权限门。
这样第 1 步的三范式 agent core 就能真正调用第 2 步的 scan（不再是 echo/add 玩具），
并且在「找项目 / 读代码」时受权限边界与授权约束（DESIGN §14）。

权限分两级：
  - find_repo（浏览目录名，低风险）：在 workspace_root 内免授权。
  - scan（读取代码内容，高风险）：需先经 PermissionBroker 授权。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from sentinel.engines.agent import Tool
from sentinel.engines.scan import _SKIP_DIRS, scan_repo, signals_of
from sentinel.permissions import PermissionBroker

# ---- 工具描述（双语 · 已与用户共审 · DESIGN §15）------------------------------
# LLM 靠这些文字决定「何时调用、传什么参数」。

_FIND_DESC = (
    "find_repo(关键词) — 在允许的工作区内按名字查找项目/目录，返回匹配的绝对路径列表。"
    "当用户只给了项目名（而非完整绝对路径）时，先用它把名字解析成真实路径。"
    "只浏览目录名，不读取文件内容，无需授权。 | "
    "find_repo(keyword) — Locate a project/directory by name within the allowed workspace; "
    "returns a list of matching absolute paths. Use it to resolve a bare project name into a "
    "real path before scanning. Lists directory names only; no file contents, no permission needed."
)

_SCAN_DESC = (
    "scan(仓库路径) — 扫描代码仓库，找出可观测性盲区：调用了外部依赖"
    "（缓存/数据库/HTTP/队列等）却没有任何日志/指标/追踪的函数。"
    "输入：仓库的绝对路径（可先用 find_repo 得到）。会读取文件内容，需先获得授权；"
    "若未授权，返回 permission_required，请把该路径告知用户并请求同意。 | "
    "scan(repo_path) — Scan a code repository to find observability blind spots: functions that "
    "call external dependencies (cache/db/http/queue/...) but have no logging/metrics/tracing. "
    "Input: absolute repo path (use find_repo first if needed). Reads file contents, so it "
    "requires prior authorization; if not authorized it returns permission_required — relay the "
    "path to the user and ask for consent."
)

# 单次汇报的盲区上限，避免把超大仓库的结果整个塞进 LLM 上下文（容错 · DESIGN §13）。
_MAX_REPORTED = 30
# find_repo 限制：搜索深度与返回条数，避免在大目录树里跑飞。
_FIND_MAX_DEPTH = 4
_FIND_MAX_HITS = 20


def _clean(arg: str) -> str:
    """清洗工具输入：去空白与包裹的引号。"""
    return (arg or "").strip().strip('"').strip("'")


# ---- find_repo：在 scope 内按名字找目录（免授权）-----------------------------

def build_find_repo_tool(broker: PermissionBroker) -> Tool:
    def _find(query: str) -> Dict[str, Any]:
        q = _clean(query).lower()
        if not q:
            raise ValueError("需要查找关键词 | query required")
        root = broker.root
        # 收集 (rank, depth, path)：rank 0=精确名 1=前缀 2=子串。
        hits = []
        for dirpath, dirnames, _files in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= _FIND_MAX_DEPTH:
                dirnames[:] = []  # 限深，别深挖
            # 跳过生成物/依赖目录 + 隐藏目录（.sentinel 之类是配置，不是项目）。
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for d in dirnames:
                name = d.lower()
                if name == q:
                    rank = 0
                elif name.startswith(q):
                    rank = 1
                elif q in name:
                    rank = 2
                else:
                    continue
                hits.append((rank, depth, os.path.join(dirpath, d)))

        # 有精确同名匹配时，只保留精确匹配（「sentinel-sample-app」→ 就它一个）。
        if any(h[0] == 0 for h in hits):
            hits = [h for h in hits if h[0] == 0]
        hits.sort(key=lambda h: (h[0], h[1], h[2]))  # 精确>前缀>子串，再浅层优先
        ranked = [h[2] for h in hits]
        # 去嵌套：某匹配若在另一个匹配目录之内，丢弃深层的（保留顶层项目）。
        kept = [p for p in ranked
                if not any(p != k and p.startswith(k + os.sep) for k in ranked)]
        return {"query": query, "root": root, "matches": kept[:_FIND_MAX_HITS]}

    return Tool("find_repo", _FIND_DESC, _find)


# ---- scan：读取代码内容（需授权）--------------------------------------------

def build_scan_tool(broker: Optional[PermissionBroker] = None) -> Tool:
    """构造 scan 工具。

    broker=None 时不做权限门（供 CLI 等「用户显式发起 = 已隐含同意」的场景）。
    传入 broker 时（如 Web 会话）：越界拒绝、未授权返回 permission_required。
    """
    def _scan(path: str) -> Dict[str, Any]:
        p = _clean(path)
        if not p:
            raise ValueError("需要仓库路径 | repo path required")
        if not os.path.exists(p):
            raise FileNotFoundError(f"路径不存在 | path not found: {p}")

        if broker is not None:
            if not broker.within_scope(p):
                return {"denied": os.path.abspath(p),
                        "reason": "超出允许的工作区范围，拒绝访问 | out of allowed workspace"}
            if not broker.is_granted(p):
                return {"permission_required": os.path.abspath(p),
                        "reason": "读取该目录代码需要用户授权 | consent needed to read this code"}

        result = scan_repo(p)
        spots = result.blind_spots
        return {
            "repo": p,
            "total_units": len(result.units),
            "blind_spot_count": len(spots),
            "blind_spots": [
                {
                    "file": u.file,
                    "function": u.qualname,
                    "signals": signals_of(u),
                    "lines": f"{u.start_line}-{u.end_line}",
                }
                for u in spots[:_MAX_REPORTED]
            ],
        }

    return Tool("scan", _SCAN_DESC, _scan)
