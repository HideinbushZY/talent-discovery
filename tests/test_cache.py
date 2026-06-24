"""TTL 缓存 / 限速 / 限并发（app/cache.py）。"""
import time

from app.cache import RateLimiter, TTLCache, gather_limited


def test_ttl_cache_hit_and_miss():
    c = TTLCache(ttl=1000)
    c.set("k", 42)
    assert c.get("k") == 42
    assert c.get("missing") is None


def test_ttl_cache_expiry():
    c = TTLCache(ttl=-1)          # 一切立即过期
    c.set("k", 1)
    assert c.get("k") is None


async def test_gather_limited_order_and_errors():
    async def worker(x):
        if x == 3:
            raise ValueError("boom")
        return x * 2
    out = await gather_limited([1, 2, 3, 4], worker, concurrency=2)
    assert out == [2, 4, None, 8]


async def test_rate_limiter_delays_over_limit():
    rl = RateLimiter(rate=2, period=0.3)
    t0 = time.monotonic()
    await rl.acquire()
    await rl.acquire()           # 填满窗口
    await rl.acquire()           # 第三次应被节流 ~period
    assert time.monotonic() - t0 >= 0.25
