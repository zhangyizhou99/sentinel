"""LLM 客户端 —— Agent 的地基。

把「怎么调模型」封装成一个方法 complete(system, user)。
上层（范式、工具、记忆）都只依赖这个抽象，不关心底层是哪家 API。

优雅降级：没装 openai 或没配 key 时，available=False，不抛异常，
让程序仍能以「离线模式」运行（后续很多静态能力本就不需要 LLM）。
"""
from __future__ import annotations

from typing import Optional

from sentinel.config import LLMConfig


class LLMClient:
    def __init__(self, config: Optional[LLMConfig] = None):
        # 不传就从环境变量组装
        self.config = config or LLMConfig.from_env()
        self._client = None
        self._error: Optional[str] = None
        self._init()

    @property
    def available(self) -> bool:
        """当前是否真的能调用 LLM。"""
        return self._client is not None

    def why_unavailable(self) -> Optional[str]:
        """不可用时的可读原因。"""
        return self._error

    def complete(self, system: str, user: str) -> str:
        """最基础的一次问答：给系统提示 + 用户输入，返回文本。"""
        if not self.available:
            raise RuntimeError(f"LLM 不可用：{self._error}")
        resp = self._client.chat.completions.create(  # type: ignore[union-attr]
            model=self.config.model,
            temperature=self.config.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    def _init(self) -> None:
        """尝试初始化底层客户端；任何缺失都记为原因，不抛异常。"""
        if not self.config.api_key:
            self._error = (
                f"缺少 API key（provider={self.config.provider}）；"
                f"请复制 .env.example 为 .env 并填写"
            )
            return
        try:
            from openai import OpenAI  # 可选依赖
        except ImportError:
            self._error = "未安装 openai 包（pip install openai）"
            return
        self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
