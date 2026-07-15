"""配置与模型提供方预设。

设计要点：
- 密钥只从环境变量 / .env 读取，绝不硬编码（企业级安全底线）。
- 用「provider 预设」屏蔽各家差异，上层只认一个统一的 LLMConfig。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

# 各家 OpenAI 兼容服务的预设：官方 base_url + 读哪个环境变量拿 key + 默认模型。
# 想接新家，这里加一行即可。
PROVIDERS: Dict[str, Dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
    },
}


@dataclass
class LLMConfig:
    """一份与具体厂商无关的模型配置。"""
    provider: str = "openai"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量（含 .env）组装配置。

        key 的取值优先级：SENTINEL_API_KEY（统一）> provider 官方环境变量。
        """
        # 先加载 .env，让密钥留在文件里而不是 shell 历史里。
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # 没装 dotenv 就直接读系统环境变量

        provider = os.getenv("SENTINEL_PROVIDER", "openai").lower()
        preset = PROVIDERS.get(provider, {})

        api_key = os.getenv("SENTINEL_API_KEY")
        if not api_key and preset:
            api_key = os.getenv(preset["api_key_env"])

        return cls(
            provider=provider,
            api_key=api_key,
            base_url=os.getenv("SENTINEL_BASE_URL") or preset.get("base_url"),
            model=os.getenv("SENTINEL_MODEL") or preset.get("default_model"),
            temperature=float(os.getenv("SENTINEL_TEMPERATURE", "0.2")),
        )
