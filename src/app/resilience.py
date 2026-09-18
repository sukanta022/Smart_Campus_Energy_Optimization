"""Resilience primitives: rate limit, circuit breaker, LLM->fallback wiring."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

import pybreaker
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

T = TypeVar("T")


class InMemoryTokenBucket:
    """Simple per-IP token bucket. Used when Redis is not configured."""

    def __init__(self, rate_per_minute: int) -> None:
        self._rate = rate_per_minute / 60.0  # tokens per second
        self._capacity = float(rate_per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def take(self, key: str, tokens: float = 1.0) -> bool:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            tokens_available, last_refill = self._buckets.get(key, (self._capacity, now))
            elapsed = max(0.0, now - last_refill)
            tokens_available = min(self._capacity, tokens_available + elapsed * self._rate)
            if tokens_available < tokens:
                self._buckets[key] = (tokens_available, now)
                return False
            tokens_available -= tokens
            self._buckets[key] = (tokens_available, now)
            return True


class RedisTokenBucket:
    """Redis-backed token bucket using a Lua script for atomicity."""

    _LUA = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local tokens = tonumber(ARGV[3])
    local now = tonumber(ARGV[4])
    local data = redis.call('HMGET', key, 'tokens', 'ts')
    local avail = tonumber(data[1]) or capacity
    local ts = tonumber(data[2]) or now
    local elapsed = math.max(0, now - ts)
    avail = math.min(capacity, avail + elapsed * rate)
    if avail < tokens then
        redis.call('HMSET', key, 'tokens', avail, 'ts', now)
        redis.call('EXPIRE', key, 60)
        return 0
    end
    avail = avail - tokens
    redis.call('HMSET', key, 'tokens', avail, 'ts', now)
    redis.call('EXPIRE', key, 60)
    return 1
    """

    def __init__(self, redis: Redis, rate_per_minute: int) -> None:
        self._redis = redis
        self._rate = rate_per_minute / 60.0
        self._capacity = float(rate_per_minute)
        self._script = self._redis.register_script(self._LUA)

    async def take(self, key: str, tokens: float = 1.0) -> bool:
        result = await self._script(keys=[key], args=[self._rate, self._capacity, tokens, asyncio.get_event_loop().time()])
        return int(result) == 1


def build_circuit_breaker(fail_max: int, reset_timeout: int) -> pybreaker.CircuitBreaker:
    return pybreaker.CircuitBreaker(fail_max=fail_max, reset_timeout=reset_timeout)


async def with_fallback(
    primary: Callable[[], Awaitable[T]],
    fallback: Callable[[], Awaitable[T]],
    *,
    breaker: pybreaker.CircuitBreaker,
    label: str,
) -> tuple[T, str]:
    """Run primary under the circuit breaker; on failure run fallback.

    Returns (result, source) where source is "primary" or "fallback".
    """
    try:
        result = await breaker.call_async(primary)
        return result, "primary"
    except pybreaker.CircuitBreakerError as e:
        logger.warning("circuit open for %s: %s", label, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("primary %s failed: %s", label, e)
    result = await fallback()
    return result, "fallback"
