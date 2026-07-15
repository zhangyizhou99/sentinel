"""扫描器测试用的样例仓库（未埋点的 FastAPI 风格代码）。

- create_order: 调了 redis + db，且**没有**打 log → 监控盲区
- get_user: 调了 redis，但**有** logger → 不是盲区
- add: 纯计算，没碰任何依赖 → 不是监控候选
"""
import logging

import redis
import requests

logger = logging.getLogger(__name__)
r = redis.Redis()


def create_order(order):
    # 盲区：调了 redis 和外部 http，却没有任何日志/埋点
    cached = r.get(order["id"])
    resp = requests.post("http://pay/api", json=order)
    return {"cached": cached, "status": resp.status_code}


class UserService:
    def get_user(self, uid):
        # 有埋点：调了 redis，但打了 log
        logger.info("fetching user %s", uid)
        return r.get(uid)


def add(a, b):
    # 纯计算，不是监控候选
    return a + b
