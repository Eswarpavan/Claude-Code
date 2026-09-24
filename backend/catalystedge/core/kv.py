"""Small key-value store used for request budgets, TTL cache and circuit breakers.

InMemoryKV is used in tests and single-process runs; RedisKV (local Redis or
Upstash in the cloud profile) shares state between the API and the worker.
"""

from __future__ import annotations

from typing import Protocol

from catalystedge.clock import Clock, SystemClock


class KV(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, ttl_s: int) -> None: ...
    def incr(self, key: str, ttl_s: int) -> int: ...
    def delete(self, key: str) -> None: ...


class InMemoryKV:
    def __init__(self, clock: Clock | None = None):
        self._clock = clock or SystemClock()
        self._data: dict[str, tuple[bytes, float]] = {}

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def get(self, key: str) -> bytes | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        value, expires = hit
        if expires <= self._now():
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self._data[key] = (value, self._now() + ttl_s)

    def incr(self, key: str, ttl_s: int) -> int:
        current = self.get(key)
        n = int(current or b"0") + 1
        expires = self._data[key][1] if current is not None else self._now() + ttl_s
        self._data[key] = (str(n).encode(), expires)
        return n

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RedisKV:
    def __init__(self, url: str, prefix: str = "ce:"):
        import redis

        self._r = redis.Redis.from_url(url)
        self._p = prefix

    def get(self, key: str) -> bytes | None:
        return self._r.get(self._p + key)

    def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self._r.set(self._p + key, value, ex=ttl_s)

    def incr(self, key: str, ttl_s: int) -> int:
        pipe = self._r.pipeline()
        pipe.incr(self._p + key)
        pipe.expire(self._p + key, ttl_s, nx=True)
        return int(pipe.execute()[0])

    def delete(self, key: str) -> None:
        self._r.delete(self._p + key)
