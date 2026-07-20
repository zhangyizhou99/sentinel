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
import re
from typing import Any, Dict, Optional

from sentinel.engines.agent import Tool
from sentinel.engines.scan import _SKIP_DIRS, scan_repo, signals_of
from sentinel.permissions import PermissionBroker

# ---- 工具描述（双语 · 已与用户共审 · DESIGN §15）------------------------------
# LLM 靠这些文字决定「何时调用、传什么参数」。

_FIND_DESC = (
    "find_repo(关键词) — 在允许的工作区内按名字查找项目/目录，返回匹配的绝对路径列表。"
    "当用户只给了项目名（而非完整绝对路径）时，先用它把名字解析成真实路径。"
    "结果里的 children 会列出每个匹配目录下**真实存在**的一级子目录（如 backend/frontend）。"
    "若用户的话里带了限定词（如「前端/后端/frontend/backend/web/mobile/客户端/服务端」等——"
    "这类词五花八门、没有固定表，靠你自己按语义判断），你需要据此从 children 里选出对应的"
    "子目录，把「匹配目录/子目录名」这个真实路径交给 scan——只能选 children 里列出的真实项，"
    "禁止自己拼造不存在的子路径。若 children 为空或没有对应子目录，就用匹配目录本身。 | "
    "find_repo(keyword) — Locate a project/directory by name within the allowed workspace; "
    "returns a list of matching absolute paths. Use it to resolve a bare project name into a "
    "real path before scanning. `children` lists the REAL immediate subdirectories of each match "
    "(e.g. backend/frontend). If the user's phrasing implies a qualifier (frontend/backend/web/"
    "mobile/client/server/前端/后端/... — an open-ended set, judge it semantically yourself), pick "
    "the matching subdirectory from `children` and pass 'match/subdir' to scan — only choose from "
    "the real entries listed, never invent a path. If there's no matching child, use the match "
    "directory as-is. Lists directory names only; no file contents, no permission needed."
        "If matches is empty, inspect scope_hint and tell the user the project may be outside the "
        "allowed workspace; ask whether they want to expand SENTINEL_WORKSPACE_ROOT and restart."
)

_SCAN_DESC = (
    "scan(仓库路径) — 扫描代码仓库，找出可观测性盲区：调用了外部依赖"
    "（缓存/数据库/HTTP/队列等）却没有任何日志/指标/追踪的函数。"
    "输入：仓库的绝对路径（可先用 find_repo 得到）。会读取文件内容，需先获得授权；"
    "若未授权，返回 permission_required，请把该路径告知用户并请求同意。"
    "返回里若有 language_gap，说明部分文件因为还没装对应语言的解析器而被跳过——"
    "请把这个缺口告诉用户，并在用户同意后调用 install_language_support 补齐、再重新 scan。 | "
    "scan(repo_path) — Scan a code repository to find observability blind spots: functions that "
    "call external dependencies (cache/db/http/queue/...) but have no logging/metrics/tracing. "
    "Input: absolute repo path (use find_repo first if needed). Reads file contents, so it "
    "requires prior authorization; if not authorized it returns permission_required — relay the "
    "path to the user and ask for consent. If the result has language_gap, some files were "
    "skipped because no parser is installed for their language yet — tell the user, and after "
    "consent call install_language_support then re-scan."
)

_CHECK_LANG_DESC = (
    "check_language_support(仓库路径) — 检查仓库里都有哪些语言，以及 Sentinel 当前能不能扫。"
    "返回三类：supported（已能扫）、extendable（认识但还没装解析器、可人审后补齐）、"
    "unknown（不认识的扩展名）。只看文件名/扩展名，不读内容，无需授权。"
    "当扫描前想确认覆盖面、或 scan 结果疑似漏了某语言时用它。 | "
    "check_language_support(repo_path) — Report which languages a repo contains and whether "
    "Sentinel can scan them: supported (ready), extendable (known but no parser installed yet — "
    "can be added after user consent), unknown (unrecognized extensions). Reads file names only, "
    "no contents, no permission needed. Use it to confirm coverage before/after a scan."
)

# 单次汇报的盲区上限，避免把超大仓库的结果整个塞进 LLM 上下文（容错 · DESIGN §13）。
_MAX_REPORTED = 30
# find_repo 限制：搜索深度与返回条数，避免在大目录树里跑飞。
_FIND_MAX_DEPTH = 4
_FIND_MAX_HITS = 20
# 每个匹配目录最多列几个子目录（够 LLM 判断限定词，又不至于刷屏）。
_FIND_MAX_CHILDREN = 20


def _clean(arg: str) -> str:
    """清洗工具输入：去空白与包裹的引号。"""
    return (arg or "").strip().strip('"').strip("'")


def _immediate_children(path: str) -> List[str]:
    """列出一个目录的一级真实子目录（过滤生成物/隐藏目录），供 LLM 语义挑选，不臆造路径。"""
    try:
        names = sorted(
            d for d in os.listdir(path)
            if d not in _SKIP_DIRS and not d.startswith(".")
            and os.path.isdir(os.path.join(path, d))
        )
    except OSError:
        return []
    return names[:_FIND_MAX_CHILDREN]


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
        kept = kept[:_FIND_MAX_HITS]
        # children：每个匹配目录下真实存在的一级子目录（结构性事实，确定性列出）。
        # LLM 据此做语义判断（如「前端」该选哪个），只能从这些真实项里选，不能凭空造路径。
        children = {p: c for p in kept if (c := _immediate_children(p))}
        result = {"query": query, "root": root, "matches": kept, "children": children}
        if not kept:
            result["scope_hint"] = (
                f"未在允许范围 {root} 内找到 {query!r}。项目可能位于范围外；"
                "请询问用户是否要将 SENTINEL_WORKSPACE_ROOT 调整为包含该项目的父目录，"
                "并在重启 Sentinel 后重新搜索。"
            )
        return result

    return Tool("find_repo", _FIND_DESC, _find)


# ---- scan：读取代码内容（需授权）--------------------------------------------

def build_scan_tool(broker: Optional[PermissionBroker] = None, memory=None, notes=None) -> Tool:
    """构造 scan 工具。

    broker=None 时不做权限门（供 CLI 等「用户显式发起 = 已隐含同意」的场景）。
    传入 broker 时（如 Web 会话）：越界拒绝、未授权返回 permission_required。
    传入 memory（EpisodicMemory）时：①抑制用户此前标为 ignore 的函数（反馈学习）；
    ②把本次运行记入情节记忆流水。
    传入 notes（NoteStore）时：③顺带学习项目埋点约定（入乡随俗）存进语义记忆（DESIGN §8.1）。
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

        # 反馈学习：把用户此前标为「不用埋点」的函数从盲区里抑制掉（被拒的别再烦）。
        suppressed = 0
        if memory is not None:
            ignored = memory.ignored_units(p)
            if ignored:
                kept = [u for u in spots if u.unit_id not in ignored]
                suppressed = len(spots) - len(kept)
                spots = kept

        report = {
            "repo": p,
            "total_units": len(result.units),
            "blind_spot_count": len(spots),
            "suppressed_count": suppressed,
            "blind_spots": [
                {
                    "file": u.file,
                    "function": u.qualname,
                    "unit_id": u.unit_id,
                    "signals": signals_of(u),
                    "lines": f"{u.start_line}-{u.end_line}",
                }
                for u in spots[:_MAX_REPORTED]
            ],
        }
        # 语言缺口可见性：有些文件因为没装对应解析器被**静默跳过**——这里显式报出来，
        # 别让用户以为"扫完了/没盲区"，其实一大块代码根本没被看过。
        try:
            from sentinel.scanners.catalog import analyze_repo
            gap = analyze_repo(p)
            if gap.extendable:
                report["language_gap"] = dict(gap.extendable)  # 语言 → 文件数（可补齐）
        except Exception:  # noqa: BLE001  语言缺口检测失败不影响扫描主结果
            pass
        if memory is not None:
            memory.record_run(p, blind_spot_count=len(spots), suppressed_count=suppressed)
        # 入乡随俗：顺带学项目埋点约定（语义记忆）——失败不影响扫描主结果。
        if notes is not None:
            try:
                from sentinel.engines.conventions import learn_and_store
                learn_and_store(p, result.units, notes)
            except Exception:  # noqa: BLE001
                pass
        return report

    return Tool("scan", _SCAN_DESC, _scan)


# ---- apply_instrumentation：提议给盲区补埋点（破坏性，但只提议不改代码）------------

_APPLY_DESC = (
    "apply_instrumentation(targets, repo) —— 【给盲区补埋点，会直接改代码文件，破坏性】"
    "对仓库监控盲区函数补埋点，改动**直接写回源文件、未提交**（不建分支；用户在编辑器里 review，"
    "满意就自己 commit，不满意 `git checkout -- 文件` 撤销）。可多次增量调用：先补几个、再补几个。\n"
    "参数：targets=补哪些（留空=全部；或逗号分隔的函数名/关键词，如 'checkin' 或 'checkin,load_cargo'）——"
    "用户说「只补第一个 / 前两个 / 除了 X 都补」时，你要先看扫描结果把它对应到具体函数名再填；"
    "repo=仓库路径（一般是最近扫的，可留空）。\n"
    "仅当用户明确要补埋点/修复盲区时调用。Python 会遵循项目日志约定；JS/TS/TSX 仅在项目"
    "已有官方 Grafana Faro SDK + recordObservability/pushEvent helper 时自动改写，绝不以"
    "console.info 冒充遥测。结果会分别报告 emitter、receiver_configured 与 delivery；"
    "源码已改不等于 Grafana 已收到。其它语言可扫描，但没有可验证 emitter 时会安全拒绝改写。 | "
    "apply_instrumentation: instrument blind-spot functions by editing files in place (uncommitted, no branch)."
)


def _select_targets(spots, targets: str):
    """按 targets 从盲区里选出要补的（确定性；意图理解由 LLM 在上游完成）。

    空/all=全部；纯数字=第 N 个；否则按逗号分隔的关键词子串匹配 unit_id。
    """
    t = (targets or "").strip()
    if not t or t.lower() in ("all", "全部", "所有", "全部盲区"):
        return list(spots)
    if t.isdigit():
        n = int(t)
        return list(spots[n - 1:n]) if 1 <= n <= len(spots) else []
    keys = [k.strip().lower() for k in re.split(r"[,，、\s]+", t) if k.strip()]
    selected = [u for u in spots if any(k in u.unit_id.lower() for k in keys)]
    if selected:
        return selected
    # 用户常说「补 queue.ts 里的几个」，文件名不是函数名时应展开为该文件全部盲区。
    normalized = t.replace("\\", "/").lower()
    return [u for u in spots if u.file.replace("\\", "/").lower().endswith(normalized)]


def build_apply_tool(broker: Optional[PermissionBroker] = None, memory=None,
                     notes=None, procedural=None, llm=None) -> Tool:
    """构造 apply_instrumentation 工具（**结构化执行器**）。

    LLM 从用户的话 + 扫描结果里填 targets（意图理解归 LLM）；工具按 targets 确定性过滤
    盲区并真执行 Applier（**直接改代码到工作区文件、未提交**）。支持多次增量补：先补几个、再补几个。
    安全网 = 未提交 + diff 预览（用户 review 后 commit / `git checkout --` 丢弃）。
    """
    def _apply(args) -> Dict[str, Any]:
        if not isinstance(args, dict):
            args = {"targets": str(args)}
        repo = _clean(args.get("repo", "")) or (memory.last_repo if memory else "") or ""
        if not repo:
            return {"error": "不知道要给哪个仓库补埋点，请先扫描或指明仓库"}
        if not os.path.exists(repo):
            return {"error": f"路径不存在: {repo}"}
        if broker is not None and not broker.within_scope(repo):
            return {"denied": os.path.abspath(repo), "reason": "超出允许的工作区范围"}
        result = scan_repo(repo)
        spots = result.blind_spots
        if memory is not None:
            ignored = memory.ignored_units(repo)
            spots = [u for u in spots if u.unit_id not in ignored]
        if not spots:
            return {"error": "没有需要补埋点的盲区（或都被标记忽略）"}
        targets = (args.get("targets") or "").strip()
        selected = _select_targets(spots, targets)
        if not selected:
            return {"error": f"没找到匹配「{targets}」的盲区；可选："
                             + ", ".join(u.unit_id for u in spots[:10])}
        from sentinel.engines.apply import Applier, ApplyError
        from sentinel.engines.conventions import learn_convention
        conv = learn_convention(repo, result.units)
        try:
            res = Applier(llm=llm).apply(repo, selected, convention=conv, procedural=procedural)
        except ApplyError as e:
            return {"error": str(e)}
        return {"applied": {
            "units_fixed": res.units_fixed, "skipped": res.skipped,
            "skipped_reasons": res.skipped_reasons,
            "files_changed": res.files_changed, "diff": res.diff[:3000],
            "emitter": res.emitter,
            "receiver_configured": res.receiver_configured,
            "delivery": res.delivery,
            "delivery_note": res.delivery_note,
            "reflection": res.reflection,
            "message": res.message,
        }}

    params = {
        "type": "object",
        "properties": {
            "targets": {"type": "string", "description":
                        "补哪些盲区：留空=全部；或逗号分隔的函数名/关键词（如 checkin 或 checkin,load_cargo）。"
                        "用户说「第一个/前两个/除了X」时，先看扫描结果把它对应到具体函数名再填。"},
            "repo": {"type": "string", "description": "仓库绝对路径（一般是最近扫描的，可留空）"},
        },
    }
    return Tool("apply_instrumentation", _APPLY_DESC, _apply, parameters=params, structured=True)


_TELEMETRY_PLAN_DESC = (
    "instrument(repo) — 根据当前扫描到的盲区生成可审阅的事件命名、信号和语言计划；"
    "不改源码、不部署。要补代码用 apply_instrumentation；要生成看板用 gen_dashboard。"
)


def build_telemetry_plan_tool(broker: Optional[PermissionBroker] = None) -> Tool:
    from sentinel.engines.grafana import generate_telemetry_plan

    def _plan(repo: str) -> Dict[str, Any]:
        path = _clean(repo)
        if broker is not None and not broker.within_scope(path):
            return {"denied": os.path.abspath(path), "reason": "超出允许的工作区范围"}
        if not os.path.exists(path):
            return {"error": f"路径不存在: {path}"}
        return generate_telemetry_plan(path)

    return Tool("instrument", _TELEMETRY_PLAN_DESC, _plan)


_DASHBOARD_DESC = (
    "gen_dashboard(repo, targets, datasource_uid) —— 为【已埋点函数】生成 Grafana dashboard JSON，"
    "不部署。覆盖 sentinel 刚补的和项目本来就有 log 的函数；只有真正会发日志的函数才会进面板。"
    "targets 指定要哪些（留空=全部已埋点；或逗号分隔的函数名/关键词/文件名，如 'queue' 或 'flush,enqueue'）；"
    "datasource_uid 可留空，届时结果会明确标记为未绑定数据源。"
)


def build_dashboard_tool(broker: Optional[PermissionBroker] = None) -> Tool:
    from sentinel.engines.grafana import generate_dashboard, plan_dashboard

    def _dashboard(args) -> Dict[str, Any]:
        if not isinstance(args, dict):
            args = {"repo": str(args)}
        repo = _clean(args.get("repo", ""))
        if broker is not None and not broker.within_scope(repo):
            return {"denied": os.path.abspath(repo), "reason": "超出允许的工作区范围"}
        if not os.path.exists(repo):
            return {"error": f"路径不存在: {repo}"}
        plan = plan_dashboard(repo, _clean(args.get("targets", "")))
        return {"plan": plan, **generate_dashboard(plan, _clean(args.get("datasource_uid", "")))}

    return Tool("gen_dashboard", _DASHBOARD_DESC, _dashboard, parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库绝对路径"},
            "targets": {"type": "string", "description":
                        "要监控哪些已埋点函数：留空=全部；或逗号分隔的函数名/关键词/文件名，如 'queue'"},
            "datasource_uid": {"type": "string", "description": "Grafana datasource UID，可留空"},
        },
        "required": ["repo"],
    }, structured=True)


_DEPLOY_DASHBOARD_DESC = (
    "deploy_dashboard(repo, targets, approved) — 【破坏性】把已埋点函数的看板部署到 Grafana，"
    "内部自己生成 dashboard 再幂等 upsert，**不需要你传 dashboard JSON**。"
    "【重要·别过度确认】只要用户已表达部署意图并给了范围——包括『把这几个部署』『部署你补的/刚补的』"
    "『部署 X 和 Y』『全部部署』——就【立即】用对应 targets + approved=true 调用本工具，"
    "**不要再反问『是否全部』，也不要反问『URL/Token 配了没』**（凭据由工具自己检查，缺了会报错回来）。"
    "『这几个/你补的/刚补的』= 最近扫描或补埋点里 source=sentinel 的那些函数，把它们的函数名逗号拼进 targets。"
    "targets：逗号分隔的函数名/关键词/文件名，如 'read,write,queuedPost'；用户明说『全部』才传 'all'；"
    "**只有用户完全没提要哪些时才留空 targets**（工具会列候选让用户选，绝不默认全建）。"
    "approved：用户已明确要部署即为 true。"
)


def build_deploy_dashboard_tool(broker: Optional[PermissionBroker] = None) -> Tool:
    from sentinel.engines.grafana import plan_dashboard, generate_dashboard, deploy_dashboard

    def _deploy(args) -> Dict[str, Any]:
        if not isinstance(args, dict):
            return {"ok": False, "reason": "参数格式不对"}
        if args.get("approved") is not True:
            return {"ok": False, "reason": "部署到 Grafana 需要用户明确同意（approved=true）；未发起部署。"}
        repo = _clean(args.get("repo", ""))
        if broker is not None and not broker.within_scope(repo):
            return {"ok": False, "denied": os.path.abspath(repo), "reason": "超出允许的工作区范围"}
        if not os.path.exists(repo):
            return {"ok": False, "error": f"路径不存在: {repo}"}
        targets = _clean(args.get("targets", ""))
        # 破坏性部署不默认全建：targets 空时先列候选让用户点名（除非显式说 all）。
        if not targets:
            candidates = [event["event"] for event in plan_dashboard(repo, "").get("events", [])]
            return {
                "ok": False, "needs_targets": True, "candidates": candidates,
                "reason": (f"未指定要部署哪些，该仓库有 {len(candidates)} 个已埋点函数可建面板："
                           f"{', '.join(candidates) or '无'}。请点名要哪几个（函数名/关键词），"
                           f"或明确说“全部”。破坏性部署不默认全建。"),
            }
        plan = plan_dashboard(repo, targets)
        if not plan.get("events"):
            candidates = [event["event"] for event in plan_dashboard(repo, "").get("events", [])]
            return {"ok": False, "reason": f"没有匹配「{targets}」的已埋点函数。可选：{', '.join(candidates) or '无'}",
                    "candidates": candidates}
        built = generate_dashboard(plan, _clean(args.get("datasource_uid", "")))
        result = deploy_dashboard(built["dashboard"], _clean(args.get("folder_uid", "")))
        result["panels"] = [event["event"] for event in plan["events"]]
        result["imprecise_panels"] = built.get("imprecise_panels", [])
        return result

    return Tool("deploy_dashboard", _DEPLOY_DASHBOARD_DESC, _deploy, parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库绝对路径"},
            "targets": {"type": "string", "description":
                        "部署哪些已埋点函数的面板（必须来自用户指定）：逗号分隔的函数名/关键词/文件名，如 'queue'；用户说全部才传 'all'；留空=先列候选让用户选"},
            "datasource_uid": {"type": "string", "description": "Grafana Loki datasource UID，可留空（默认读环境变量）"},
            "folder_uid": {"type": "string", "description": "Grafana folder UID，可留空"},
            "approved": {"type": "boolean", "description": "只有用户已明确同意部署时才可为 true"},
        },
        "required": ["repo", "approved"],
    }, structured=True)


# ---- check_language_support：报告语言覆盖面与缺口（只读，免授权）--------------

def build_check_language_tool(broker: Optional[PermissionBroker] = None) -> Tool:
    """构造 check_language_support 工具：报告仓库语言与是否可扫。

    只看扩展名（不读内容），风险低。broker 存在时仅做越界检查，不需授权。
    extendable 语言表示「认识但还没装解析器」——补齐动作（install_language_support）
    是破坏性的，必须经人审门（Web 按钮），本工具只做只读的检测与提示。
    """
    from sentinel.scanners.catalog import analyze_repo

    def _check(path: str) -> Dict[str, Any]:
        p = _clean(path)
        if not p:
            raise ValueError("需要仓库路径 | repo path required")
        if not os.path.exists(p):
            raise FileNotFoundError(f"路径不存在 | path not found: {p}")
        if broker is not None and not broker.within_scope(p):
            return {"denied": os.path.abspath(p),
                    "reason": "超出允许的工作区范围 | out of allowed workspace"}

        gap = analyze_repo(p)
        return {
            "repo": p,
            "supported": gap.supported,     # 语言 → 文件数（已能扫）
            "extendable": gap.extendable,   # 语言 → 文件数（可人审后补齐）
            "unknown": gap.unknown,         # 扩展名 → 文件数（不认识）
            "needs_extension": gap.needs_extension,
            "hint": ("检测到可补齐的语言，可在获得用户同意后调用 install_language_support 补齐 | "
                     "extendable languages found; call install_language_support after user consent")
            if gap.needs_extension else "",
        }

    return Tool("check_language_support", _CHECK_LANG_DESC, _check)


# ---- install_language_support：补齐某语言解析能力（破坏性 · 人审门）-----------

_INSTALL_LANG_DESC = (
    "install_language_support(语言名) — 给 Sentinel 补上某门语言的解析能力（tree-sitter）。"
    "**这是破坏性操作**：可能 pip 安装依赖、并让 LLM 现写解析查询。"
    "⚠️ 只有在用户于对话中**明确同意补齐该语言**后才可调用；不要自作主张。"
    "先用 check_language_support 找出 extendable 语言，向用户说明并征得同意，再调用本工具。"
    "输入：语言名（如 go、java、typescript）。成功后即可 scan 该语言文件。 | "
    "install_language_support(language) — Add parsing support for a language (tree-sitter). "
    "**Destructive**: may pip-install deps and have the LLM author a parser query. "
    "⚠️ Call ONLY after the user has EXPLICITLY consented to adding this language. "
    "Use check_language_support first, explain the gap, get consent, then call this. "
    "Input: language name (e.g. go, java, typescript)."
)


def build_install_language_tool(llm=None) -> Tool:
    """构造 install_language_support 工具。破坏性，靠描述约束「用户明确同意后才调」。

    llm 用于给没有内置/缓存查询的冷门语言现写查询（编译校验兜底）。
    """
    from sentinel.scanners.treesitter_scanner import install_language_support

    def _install(language: str) -> Dict[str, Any]:
        lang = _clean(language).lower()
        if not lang:
            raise ValueError("需要语言名 | language name required")
        return install_language_support(lang, llm=llm)

    return Tool("install_language_support", _INSTALL_LANG_DESC, _install)


_REGISTER_LANG_DESC = (
    "register_dynamic_language_support(language, extensions) — 为当前未知扩展名注册新的 "
    "tree-sitter 语言支持。仅当用户明确要求补齐，并确认语言名与扩展名对应关系后调用。"
    "工具会让 LLM 生成函数/调用查询，但必须在真实 grammar 上编译通过才保存映射；失败不修改配置。"
)


def build_register_dynamic_language_tool(llm=None) -> Tool:
    """构造未知语言注册工具：扩展名映射仅在验证成功后才持久化。"""
    from sentinel.scanners.treesitter_scanner import register_dynamic_language_support

    def _register(args) -> Dict[str, Any]:
        if not isinstance(args, dict):
            return {"ok": False, "reason": "需要 language 与 extensions 参数"}
        language = _clean(args.get("language", "")).lower()
        extensions = args.get("extensions") or []
        if isinstance(extensions, str):
            extensions = [part.strip() for part in extensions.split(",")]
        if not isinstance(extensions, list):
            return {"ok": False, "reason": "extensions 必须是扩展名列表"}
        return register_dynamic_language_support(language, [str(ext) for ext in extensions], llm=llm)

    return Tool("register_dynamic_language_support", _REGISTER_LANG_DESC, _register, parameters={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "tree-sitter-language-pack 语言名，例如 vue、swift"},
            "extensions": {"type": "array", "items": {"type": "string"},
                           "description": "用户确认属于该语言的扩展名，例如 ['.vue']"},
        },
        "required": ["language", "extensions"],
    }, structured=True)


# ---- ignore_finding：把某盲区标为「不用埋点」，形成反馈学习（Agentic-RL）--------

_IGNORE_DESC = (
    "ignore_finding(函数标识) — 把某个盲区函数标记为「不需要埋点」。之后再扫同一仓库时，"
    "该函数会被自动抑制、不再报为盲区（这就是 Sentinel 的反馈学习：被你拒过的别再烦你）。"
    "输入：函数标识 unit_id，即扫描结果里的「相对文件路径::函数名」（如 svc/order.py::create_order）。"
    "仅当用户明确表示某函数不用埋点时才调用；针对的是最近一次扫描的那个仓库。 | "
    "ignore_finding(unit_id) — Mark a blind-spot function as 'no instrumentation needed'. "
    "Future scans of the same repo will suppress it (this is Sentinel's feedback learning: "
    "don't nag about what you rejected). Input: the unit_id 'relative/path.py::qualname' from "
    "the scan report. Call only when the user explicitly says a function needs no instrumentation; "
    "applies to the most recently scanned repo."
)


def build_feedback_tool(memory) -> Tool:
    """构造 ignore_finding 工具：记录「不用埋点」的裁决，驱动下次扫描抑制。

    针对 memory.last_repo（最近扫过的仓库）。没扫过则提示先扫。
    """
    from sentinel.memory import IGNORE

    def _ignore(unit_id: str) -> Dict[str, Any]:
        uid = _clean(unit_id)
        if not uid:
            raise ValueError("需要函数标识 unit_id（文件::函数名）| unit_id required")
        repo = getattr(memory, "last_repo", None)
        if not repo:
            return {"error": "还没有扫描过任何仓库，无法确定归属；请先扫描再标记 "
                             "| no repo scanned yet; scan first"}
        memory.record_feedback(repo, uid, IGNORE)
        return {"ok": True, "repo": repo, "unit_id": uid,
                "note": "已记录：下次扫描将抑制该函数 | recorded; suppressed on next scan"}

    return Tool("ignore_finding", _IGNORE_DESC, _ignore)


# ---- add_note / recall_notes：团队笔记（喂给 ContextBuilder 的一等证据）--------

_ADD_NOTE_DESC = (
    "add_note(内容) — 记下一条关于当前仓库的团队笔记/约定（如「所有外部 HTTP 调用都要打"
    "延迟直方图」「settings.py 里的读取无需埋点」）。笔记会持久保存，并在之后判定该仓库的"
    "函数时被自动召回、纳入上下文——让 Sentinel 越用越懂这个团队的规矩。"
    "输入：笔记正文；归属最近一次扫描的仓库。 | "
    "add_note(text) — Record a team note/convention about the current repo. It is persisted and "
    "auto-recalled into the judgement context for this repo's functions later. Input: the note "
    "text; scoped to the most recently scanned repo."
)

_RECALL_NOTES_DESC = (
    "recall_notes(关键词) — 查一下当前仓库有哪些相关团队笔记/约定。输入：关键词（可留空看最近的）。"
    "用于判定前先了解团队既有规矩，避免给出与约定冲突的建议。 | "
    "recall_notes(query) — Look up relevant team notes/conventions for the current repo. Input: "
    "a keyword (may be empty for recent ones). Use it to align with existing conventions."
)


def build_note_tool(notes, memory) -> Tool:
    """构造 add_note 工具：把笔记绑到 memory.last_repo（最近扫过的仓库）。"""
    def _add(text: str) -> Dict[str, Any]:
        body = _clean(text)
        if not body:
            raise ValueError("需要笔记内容 | note text required")
        repo = getattr(memory, "last_repo", None)  # 没扫过则记为全局笔记
        note_id = notes.add_note(body, repo=repo, author="user")
        scope = "仓库" if repo else "全局"
        return {"ok": True, "note_id": note_id, "scope": scope, "repo": repo or "(global)",
                "note": "已记录，之后判定该仓库函数时会自动纳入上下文 | recorded; "
                        "will be recalled into context"}

    return Tool("add_note", _ADD_NOTE_DESC, _add)


def build_recall_notes_tool(notes, memory) -> Tool:
    """构造 recall_notes 工具：按当前仓库 + 关键词召回笔记。"""
    def _recall(query: str) -> Dict[str, Any]:
        repo = getattr(memory, "last_repo", None)
        hits = notes.search_notes(repo=repo, query=_clean(query), limit=8)
        return {
            "repo": repo or "(global)",
            "count": len(hits),
            "notes": [
                {"id": sn.note.id, "scope": sn.note.scope, "tags": sn.note.tags,
                 "text": sn.note.text}
                for sn in hits
            ],
        }

    return Tool("recall_notes", _RECALL_NOTES_DESC, _recall)
