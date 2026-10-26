"""失败分类与退避策略。

原则：
- 网络抖动、超时、5xx、连接重置：可重试；
- 登录失效、参数错误、无版权/文件损坏：立即失败，不轰炸服务器；
- 限流信号（429 / “接口频繁” / -462）：退避到上限后放弃，不无限重试。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

RATE_LIMIT_WORDS = ("频繁", "too many", "rate limit", "429", "-462", "405", "406")


def is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if any(k in text for k in RATE_LIMIT_WORDS):
        return True
    if any(k in name for k in ("timeout", "connection", "reset", "ssl")):
        return True
    # requests / httpx 的 HTTPError 文本里通常带状态码
    return any(f" {code}" in text or f"http {code}" in text for code in (500, 502, 503, 504))


@dataclass
class RetryPolicy:
    attempts: int = 2
    base_s: float = 5.0
    max_s: float = 40.0
    jitter_s: float = 1.0

    def delay(self, attempt: int) -> float:
        """attempt 从 1 开始；指数退避 + 抖动，封顶 max_s。"""
        raw = min(self.max_s, self.base_s * (2 ** max(0, attempt - 1)))
        return raw + random.uniform(0, self.jitter_s)

    def sleep(self, attempt: int) -> None:
        time.sleep(self.delay(attempt))
