"""第 0 步：验证配置与 LLM 客户端（离线可跑，不联网）。

运行：PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.config import LLMConfig, PROVIDERS  # noqa: E402
from sentinel.llm import LLMClient  # noqa: E402


def test_config_from_env_picks_provider(monkeypatch):
    monkeypatch.setenv("SENTINEL_PROVIDER", "deepseek")
    monkeypatch.delenv("SENTINEL_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg = LLMConfig.from_env()
    assert cfg.provider == "deepseek"
    assert cfg.base_url == PROVIDERS["deepseek"]["base_url"]
    assert cfg.model == PROVIDERS["deepseek"]["default_model"]
    assert cfg.api_key == "sk-test"


def test_unified_key_takes_priority(monkeypatch):
    monkeypatch.setenv("SENTINEL_PROVIDER", "openai")
    monkeypatch.setenv("SENTINEL_API_KEY", "sk-unified")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-official")
    cfg = LLMConfig.from_env()
    assert cfg.api_key == "sk-unified"  # 统一 key 优先


def test_client_unavailable_without_key():
    # 显式传无 key 配置，行为确定（不依赖环境）。
    client = LLMClient(LLMConfig(provider="openai", api_key=None))
    assert client.available is False
    assert client.why_unavailable()  # 有可读原因


def test_complete_raises_when_unavailable():
    client = LLMClient(LLMConfig(provider="openai", api_key=None))
    try:
        client.complete("sys", "hi")
        assert False, "应当抛出 RuntimeError"
    except RuntimeError:
        pass
