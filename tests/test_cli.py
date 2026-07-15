"""第 0 步：验证 CLI 入口（离线可跑，不联网）。

只测最外层链路：`sentinel ping` 在没有 key 时进入离线模式并回显输入。
运行：PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.cli import build_parser, main  # noqa: E402


def test_parser_has_ping_command():
    parser = build_parser()
    # 能解析 ping 子命令且带 message 参数
    args = parser.parse_args(["ping", "你好"])
    assert args.command == "ping"
    assert args.message == "你好"


def test_ping_offline_echoes_input(monkeypatch, capsys):
    # 把所有 key 设为空串（而非删除）：这样 load_dotenv 不会用 .env 覆盖已存在的变量，
    # 确保进入离线模式（行为确定、不联网、不受本地 .env 影响）。
    for var in ("SENTINEL_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("SENTINEL_PROVIDER", "openai")

    main(["ping", "hello-sentinel"])
    out = capsys.readouterr().out
    assert "离线模式" in out
    assert "hello-sentinel" in out  # 回显了输入


def test_no_command_prints_help(capsys):
    main([])
    out = capsys.readouterr().out
    assert "sentinel" in out.lower()  # 打印了帮助
