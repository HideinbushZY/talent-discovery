"""轻量内存 TTL 缓存 + 并发/限速闸门（spec §6.3）。

单进程 demo 用，够稳；进程重启即清空。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Tuple


class TTLCache:
    def __init__(self, ttl: float = 900.0):
        self.ttl = ttl
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        ts, val = item
        if time.monotonic() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any):
        self._store[key] = (time.monotonic(), val)


class RateLimiter:
    """简单的令牌窗口：每 `period` 秒最多 `rate` 次调用。"""

    def __init__(self, rate: int, period: float = 60.0):
        self.rate = max(1, rate)
        self.period = period
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            # 丢弃窗口外的记录
            self._calls = [t for t in self._calls if now - t < self.period]
            if len(self._calls) >= self.rate:
                wait = self.period - (now - self._calls[0]) + 0.05
                await asyncio.sleep(max(0.0, wait))
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.period]
            self._calls.append(now)


async def gather_limited(
    items: list,
    worker: Callable[[Any], Awaitable[Any]],
    concurrency: int = 6,
) -> list:
    """限并发地把 worker 应用到每个 item，保持顺序，异常落为 None。"""
    sem = asyncio.Semaphore(concurrency)

    async def run(it):
        async with sem:
            try:
                return await worker(it)
            except Exception:
                return None

    return await asyncio.gather(*(run(it) for it in items))
