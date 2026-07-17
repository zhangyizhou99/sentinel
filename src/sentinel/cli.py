"""命令行入口。

第 0 步只提供一个命令：`sentinel ping "<话>"`
用来验证「配置 → LLM 客户端 → 一次调用」这条最细的链路是否打通。
没配 key 时自动进入离线模式（回显），方便先跑起来。
"""
from __future__ import annotations

import argparse
from typing import Optional

from sentinel import __version__
from sentinel.llm import LLMClient
from sentinel.engines.scan import scan_repo, signals_of
from sentinel.memory import EpisodicMemory, IGNORE, INSTRUMENT

# 系统提示：定义这个 Agent 的身份。后续会逐步丰富。
SYSTEM_PROMPT = "你是 Sentinel，一个可观测性守护 Agent 的雏形。用中文简洁回答。"


def cmd_ping(args: argparse.Namespace) -> None:
    client = LLMClient()
    if not client.available:
        print(f"[离线模式] LLM 不可用：{client.why_unavailable()}")
        print(f"[离线模式] 回显你的输入：{args.message}")
        return
    reply = client.complete(SYSTEM_PROMPT, args.message)
    print(reply)


def cmd_scan(args: argparse.Namespace) -> None:
    """扫描仓库，列出监控盲区（纯静态，不用 LLM）。

    带上情节记忆：抑制此前被标为「不用埋点」的函数（反馈学习），并记录本次运行。
    """
    memory = EpisodicMemory()
    result = scan_repo(args.repo)
    spots = result.blind_spots

    ignored = memory.ignored_units(args.repo)
    kept = [u for u in spots if u.unit_id not in ignored]
    suppressed = len(spots) - len(kept)
    spots = kept

    note = f"（已抑制 {suppressed} 个你此前标记忽略的函数）" if suppressed else ""
    print(f"扫描 {args.repo}：共 {len(result.units)} 个函数/方法，"
          f"发现 {len(spots)} 个监控盲区{note}\n")
    for u in spots:
        sigs = "/".join(signals_of(u))
        print(f"  ⚠ {u.unit_id}  [{sigs}]  行 {u.start_line}-{u.end_line}")
        print(f"     调用: {', '.join(u.calls)}")
    if not spots:
        print("  ✅ 未发现盲区（或仓库无可观测性相关调用）")
    memory.record_run(args.repo, blind_spot_count=len(spots), suppressed_count=suppressed)
    memory.close()


def cmd_feedback(args: argparse.Namespace) -> None:
    """记录/查看对某仓库的反馈（哪些函数不用埋点）。反馈会在下次 scan 时生效。"""
    memory = EpisodicMemory()
    if args.list:
        rows = memory.list_feedback(args.repo)
        if not rows:
            print(f"{args.repo} 暂无反馈记录。")
        for r in rows:
            print(f"  [{r.decision}] {r.unit_id}" + (f"  # {r.note}" if r.note else ""))
        memory.close()
        return
    if not args.unit_id:
        print("请提供 unit_id（文件::函数名），或用 --list 查看已有反馈。")
        memory.close()
        return
    decision = INSTRUMENT if args.instrument else IGNORE
    memory.record_feedback(args.repo, args.unit_id, decision)
    verb = "会重新提示埋点" if decision == INSTRUMENT else "下次扫描将抑制"
    print(f"已记录：{args.repo} :: {args.unit_id} → {decision}（{verb}）")
    memory.close()


def cmd_apply(args: argparse.Namespace) -> None:
    """对盲区函数补埋点：改代码 → 用户命名的新 git 分支，未提交（待人审）。

    走完整三记忆：情节（抑制被忽略）+ 语义（学项目埋点约定）+ 程序性（复用/记录修复技能）。
    “用户敲命令 + 必填 --branch” 即明确同意，是破坏性操作的人审门。
    """
    from sentinel.engines.apply import Applier, ApplyError
    from sentinel.engines.conventions import learn_and_store
    from sentinel.memory import NoteStore, ProceduralMemory

    memory = EpisodicMemory()
    result = scan_repo(args.repo)
    ignored = memory.ignored_units(args.repo)
    spots = [u for u in result.blind_spots if u.unit_id not in ignored]
    if not spots:
        print("没有需要补埋点的盲区（或都被标记忽略）。")
        memory.close()
        return

    notes = NoteStore()
    conv = learn_and_store(args.repo, result.units, notes)     # 入乡随俗：学并存约定
    procedural = ProceduralMemory()

    print(f"将对 {len(spots)} 个盲区补埋点，分支：{args.branch}")
    if conv.found:
        print(f"（项目埋点约定：{conv.style}）")
    try:
        res = Applier().apply(args.repo, spots, args.branch, convention=conv, procedural=procedural)
    except ApplyError as e:
        print(f"❌ 无法补埋点：{e}")
        memory.close()
        return

    print(res.message)
    if res.units_fixed:
        print(f"  ✅ 已补 {len(res.units_fixed)} 个：{', '.join(res.units_fixed)}")
    if res.skipped:
        print(f"  ⏭ 跳过 {len(res.skipped)} 个（非 Python / 改写不安全）：{', '.join(res.skipped)}")
    print(f"\n--- diff 预览（未提交，在分支 {res.branch}）---")
    print(res.diff[:2000] or "(无 diff)")
    memory.close()




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Sentinel —— 面向多人协作代码库的可观测性守护 Agent",
    )
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")

    sub = parser.add_subparsers(dest="command")
    ping = sub.add_parser("ping", help="向 LLM 发一句话，验证链路是否打通")
    ping.add_argument("message", help="要发送的内容")
    ping.set_defaults(func=cmd_ping)

    scan = sub.add_parser("scan", help="扫描仓库，列出监控盲区（纯静态，不用 LLM）")
    scan.add_argument("repo", help="仓库路径或单个 .py 文件")
    scan.set_defaults(func=cmd_scan)

    fb = sub.add_parser("feedback", help="标记某函数是否需要埋点；反馈在下次 scan 生效")
    fb.add_argument("repo", help="仓库路径")
    fb.add_argument("unit_id", nargs="?", help="函数标识：相对文件路径::函数名")
    fb.add_argument("--ignore", action="store_true", help="标为不用埋点（默认）")
    fb.add_argument("--instrument", action="store_true", help="标为需要埋点（撤销忽略）")
    fb.add_argument("--list", action="store_true", help="列出该仓库已有反馈")
    fb.set_defaults(func=cmd_feedback)

    ap = sub.add_parser("apply", help="对盲区函数补埋点（改代码 → 新 git 分支，未提交待人审）")
    ap.add_argument("repo", help="仓库路径")
    ap.add_argument("--branch", required=True, help="补埋点落在哪个新分支（须自己命名）")
    ap.set_defaults(func=cmd_apply)

    return parser


def main(argv: Optional[list] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return
    func(args)


if __name__ == "__main__":
    main()
