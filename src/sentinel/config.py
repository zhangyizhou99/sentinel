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
    # GitHub Models：用 GitHub Token 调用，OpenAI 兼容，个人有免费额度。
    # key = GitHub PAT（需勾选 models 权限）；模型名带 publisher 前缀。
    "github": {
        "base_url": "https://models.github.ai/inference",
        "api_key_env": "GITHUB_TOKEN",
        "default_model": "openai/gpt-4o-mini",
    },
    # 本地 copilot-api 代理：把 GitHub Copilot 订阅包成 OpenAI 兼容接口。
    # 订阅制计费（不按 token）；key 代理不校验，填任意值。
    # 需先启动代理：bun run ./src/main.ts start --port 4141 --rate-limit 5
    "copilot": {
        "base_url": "http://localhost:4141/v1",
        "api_key_env": "COPILOT_API_KEY",
        "default_model": "gpt-4o",
    },
}


def _load_dotenv() -> None:
    """按调用方工作目录加载 .env；系统环境变量仍保持更高优先级。"""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _dotenv_values() -> Dict[str, str]:
    """读取 .env 原始值，用于识别已被注入 os.environ 的文件配置。"""
    try:
        from dotenv import dotenv_values
        return {key: value for key, value in dotenv_values().items() if value is not None}
    except ImportError:
        return {}


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
        # 显式环境提供方（如 CI）不能继承 .env 中另一个 provider 的 URL/模型。
        env_provider = os.getenv("SENTINEL_PROVIDER")
        env_api_key = os.getenv("SENTINEL_API_KEY")
        env_base_url = os.getenv("SENTINEL_BASE_URL")
        env_model = os.getenv("SENTINEL_MODEL")
        file_values = _dotenv_values()
        overrides_file_provider = bool(
            env_provider and env_provider != file_values.get("SENTINEL_PROVIDER")
        )
        # 先加载 .env，让密钥留在文件里而不是 shell 历史里。
        _load_dotenv()

        provider = (env_provider or os.getenv("SENTINEL_PROVIDER", "openai")).lower()
        preset = PROVIDERS.get(provider, {})

        api_key = env_api_key
        if overrides_file_provider and api_key == file_values.get("SENTINEL_API_KEY"):
            api_key = None
        if not api_key and not env_provider:
            api_key = os.getenv("SENTINEL_API_KEY")
        if not api_key and preset:
            api_key = os.getenv(preset["api_key_env"])

        base_url = env_base_url
        model = env_model
        if overrides_file_provider:
            if base_url == file_values.get("SENTINEL_BASE_URL"):
                base_url = None
            if model == file_values.get("SENTINEL_MODEL"):
                model = None
        if not env_provider:
            base_url = base_url or os.getenv("SENTINEL_BASE_URL")
            model = model or os.getenv("SENTINEL_MODEL")

        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url or preset.get("base_url"),
            model=model or preset.get("default_model"),
            temperature=float(os.getenv("SENTINEL_TEMPERATURE", "0.2")),
        )


@dataclass(frozen=True)
class LocalIdentity:
    """本地协作原型的稳定身份与工作区边界。"""

    user_id: str
    display_name: str
    workspace_id: str
    workspace_name: str

    @classmethod
    def from_env(cls) -> "LocalIdentity":
        _load_dotenv()
        return cls(
            user_id=os.getenv("SENTINEL_USER_ID", "local-user").strip(),
            display_name=os.getenv("SENTINEL_USER_NAME", "本地用户").strip(),
            workspace_id=os.getenv("SENTINEL_WORKSPACE_ID", "local-workspace").strip(),
            workspace_name=os.getenv("SENTINEL_WORKSPACE_NAME", "本地工作区").strip(),
        )


def workspace_root() -> str:
    """Agent 被允许「找项目 / 读代码」的根目录（权限边界 · DESIGN §14）。

    默认 = 启动 Sentinel 的当前目录（直觉：它只看你把它放进去的那个工作区）；
    可用 SENTINEL_WORKSPACE_ROOT 覆盖。所有文件访问都不得越出这个根。
    """
    _load_dotenv()
    return os.path.abspath(os.path.expanduser(
        os.getenv("SENTINEL_WORKSPACE_ROOT") or os.getcwd()
    ))


def cache_dir() -> str:
    """Sentinel 的缓存/状态目录（记忆、查询缓存等都放这）。

    默认 ~/.cache/sentinel，可用 SENTINEL_CACHE_DIR 覆盖。放用户目录而非仓库内，
    避免污染被扫描的目标仓库。首次访问时确保目录存在。
    """
    path = os.path.abspath(os.path.expanduser(
        os.getenv("SENTINEL_CACHE_DIR") or os.path.join("~", ".cache", "sentinel")
    ))
    os.makedirs(path, exist_ok=True)
    return path


def episodic_db_path() -> str:
    """情节记忆（SQLite）文件路径：记录每次运行与用户反馈（DESIGN §11 Agentic-RL）。"""
    return os.path.join(cache_dir(), "episodic.db")
def tool_call_log_path() -> str:
    """工具调用审计日志路径；JSONL，便于按 call_id 追踪原始异常。"""
    return os.path.abspath(os.path.expanduser(
        os.getenv("SENTINEL_TOOL_CALL_LOG")
        or os.path.join(cache_dir(), "tool-calls.jsonl")
    ))


