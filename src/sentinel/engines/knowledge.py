"""RED/USE 可观测性知识库（judge_intent 的证据来源之一）。

RED（Rate/Errors/Duration）与 USE（Utilization/Saturation/Errors）是业界判断
「一个依赖调用该监控什么」的经验法则。这里把「信号 → 推荐观测」做成一张小静态表，
作为 judge_intent 的**确定性证据**（不是 LLM 编的，可引用为 `knowledge:<信号>`）。

将来可外置成可编辑/可学习的知识库；现阶段内置够用（DESIGN §8 Semantic 层雏形）。
"""
from __future__ import annotations

from typing import Dict, List

# 信号（与 engines.scan.OBS_SIGNALS 的值域一致）→ 推荐观测项。
RED_USE_KNOWLEDGE: Dict[str, List[str]] = {
    "http":    ["请求耗时（latency 直方图）", "错误率（4xx/5xx）", "调用次数（QPS）"],
    "db":      ["查询耗时", "错误/异常数", "慢查询/影响行数"],
    "cache":   ["命中率（hit/miss）", "读写延迟"],
    "queue":   ["收/发消息数", "消费延迟与积压（lag）", "失败/重试数"],
    "cloud":   ["调用耗时", "错误数", "传输字节数/对象大小"],
    "network": ["连接耗时", "超时/失败数"],
}


def knowledge_for(signals: List[str]) -> Dict[str, List[str]]:
    """取给定信号对应的推荐观测（只返回命中的信号）。"""
    return {sig: RED_USE_KNOWLEDGE[sig] for sig in signals if sig in RED_USE_KNOWLEDGE}
