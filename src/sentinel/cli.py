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
