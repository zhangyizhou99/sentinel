"""可观测性信号词典 —— 多语言「这个函数碰了外部依赖吗」的判据（三层召回漏斗的 L1）。

同一个信号（http/db/cache/cloud/queue/network）在不同生态里库名/调用名不同，所以按
**语言分包**。子串匹配快、免费、air-gapped，覆盖**直接调用**。封装/语义化的调用 L1 够不着，
交给 L2（调用图传播）/ L3（LLM 语义兜底）补，见 DESIGN §2.2。

独立小模块：不 import 任何 scanner / engine，避免循环导入（与 instrumentation.py 一致）。
"""
from __future__ import annotations

from typing import Dict, List

# ---- Python 生态：直接库名 ----------------------------------------------------
_PY_SIGNALS: Dict[str, str] = {
    "redis": "cache", "memcache": "cache",
    "execute": "db", "query": "db", "cursor": "db", "session": "db",
    "sqlalchemy": "db", "psycopg": "db", "pymysql": "db", "sqlite": "db",
    "requests": "http", "httpx": "http", "urllib": "http", "aiohttp": "http", "urlopen": "http",
    "boto3": "cloud", "kafka": "queue", "pika": "queue", "celery": "queue",
    "socket": "network",
}

# ---- JS/TS 生态：网络方式极多样，尽量覆盖主流 + 常见封装名 -------------------------
# 注意：子串匹配是「宁可多召回」——`fetch` 也会命中 `fetchWaybills`（好，抓封装）
# 但同样会命中 `prefetch`（噪声）。L1 接受一定假阳，由 L3 语义精修。
_JS_SIGNALS: Dict[str, str] = {
    # http / 网络
    "fetch": "http", "axios": "http", "xmlhttprequest": "http", "xhr": "http",
    "ajax": "http", "superagent": "http", "got": "http", "ky": "http",
    "httpclient": "http", "apollo": "http", "graphql": "http", "urql": "http",
    "trpc": "http", "usequery": "http", "usemutation": "http", "useswr": "http",
    "swr": "http", "request": "http",
    # db
    "prisma": "db", "knex": "db", "typeorm": "db", "sequelize": "db",
    "mongoose": "db", "mongodb": "db", "supabase": "db", "firestore": "db",
    "drizzle": "db", "indexeddb": "db",
    # cache / 本地存储
    "redis": "cache", "ioredis": "cache", "localstorage": "cache", "sessionstorage": "cache",
    # cloud
    "s3": "cloud", "aws-sdk": "cloud", "presigned": "cloud", "getsignedurl": "cloud",
    "cloudinary": "cloud",
    # queue
    "kafka": "queue", "amqp": "queue", "rabbitmq": "queue", "sqs": "queue",
    "pubsub": "queue", "bull": "queue",
}

# language → { 子串: 信号类别 }
SIGNAL_WORDS: Dict[str, Dict[str, str]] = {
    "python": _PY_SIGNALS,
    "javascript": _JS_SIGNALS,
    "typescript": _JS_SIGNALS,
    "tsx": _JS_SIGNALS,
}

# 未知语言的兜底：所有词的并集（宁可多召回，再由 L3 精修）。
_ALL_WORDS: Dict[str, str] = {}
for _m in SIGNAL_WORDS.values():
    _ALL_WORDS.update(_m)


def signals_for_language(language: str) -> Dict[str, str]:
    """取某语言的信号词典；未知语言返回全语言并集（宽松召回）。"""
    if not language:
        return _ALL_WORDS
    return SIGNAL_WORDS.get(language.lower(), _ALL_WORDS)


def signals_in_calls(calls: List[str], language: str = "") -> List[str]:
    """给一串调用点号名 + 语言，返回命中的信号类别（去重排序）。"""
    words = signals_for_language(language)
    found = set()
    for call in calls:
        low = (call or "").lower()
        for key, sig in words.items():
            if key in low:
                found.add(sig)
    return sorted(found)
