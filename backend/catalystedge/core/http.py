"""Polite HTTP client shared by every adapter.

Per source it enforces: minimum spacing between calls, a daily request budget,
a TTL response cache, retries with exponential backoff (honouring
Retry-After) on 429/5xx/network errors, and a circuit breaker that stops
calling a source that keeps failing. API keys are redacted from every error
message and log line. There is no proxy rotation, user-agent spoofing or
other anti-bot evasion: every request declares CatalystEdge.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from catalystedge.clock import Clock, SystemClock
from catalystedge.core.kv import KV, InMemoryKV
from catalystedge.sources import SOURCES, SourceSpec

USER_AGENT = "CatalystEdge/0.1 (personal research; contact via SEC_USER_AGENT)"
SECRET_PARAMS = {"token", "api_token", "apikey", "api_key"}
RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(Exception):
    """Base class: something about this source prevented a result."""

    def __init__(self, source: str, message: str):
        super().__init__(f"{source}: {message}")
        self.source = source


class BudgetExhausted(SourceError):
    pass


class CircuitOpen(SourceError):
    pass


class QuotaExceeded(SourceError):
    """The provider itself said we are over its limit (even with HTTP 200)."""


class ProviderError(SourceError):
    def __init__(self, source: str, message: str, status: int | None = None):
        super().__init__(source, message)
        self.status = status


@dataclass
class CallStats:
    http_calls: int = 0
    cache_hits: int = 0
    retries: int = 0


@dataclass
class HttpClient:
    transport: httpx.BaseTransport | None = None
    kv: KV = field(default_factory=InMemoryKV)
    clock: Clock = field(default_factory=SystemClock)
    sleep: Callable[[float], None] = time.sleep
    specs: Mapping[str, SourceSpec] = field(default_factory=lambda: SOURCES)
    max_attempts: int = 3
    breaker_threshold: int = 5
    breaker_cooldown_s: int = 600
    timeout_s: float = 20.0
    user_agent: str = USER_AGENT
    stats: dict[str, CallStats] = field(default_factory=dict)
    _last_call: dict[str, float] = field(default_factory=dict)
    _secrets: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._client = httpx.Client(transport=self.transport, timeout=self.timeout_s, follow_redirects=True)

    # ------------------------------------------------------------------ public

    def register_secret(self, value: str | None) -> None:
        if value:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def get_json(self, source: str, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None, *, ttl_s: int | None = None) -> Any:
        spec = self.specs[source]
        params = dict(params or {})
        for k in SECRET_PARAMS & params.keys():
            self.register_secret(str(params[k]))
        stats = self.stats.setdefault(source, CallStats())

        cache_key = self._cache_key(source, url, params)
        ttl = spec.cache_ttl_s if ttl_s is None else ttl_s
        if ttl > 0 and (hit := self.kv.get(cache_key)) is not None:
            stats.cache_hits += 1
            return json.loads(hit)

        self._check_circuit(source)

        last_error: SourceError | None = None
        for attempt in range(self.max_attempts):
            last_attempt = attempt == self.max_attempts - 1
            self._spend_budget(source, spec)  # retries cost provider quota too
            self._space(source, spec)
            stats.http_calls += 1
            try:
                resp = self._client.get(url, params=params, headers=self._headers(headers))
            except httpx.HTTPError as e:
                last_error = ProviderError(source, self.redact(f"network error: {type(e).__name__}: {e}"))
                if not last_attempt:
                    self._backoff(attempt, None, stats)
                continue
            if resp.status_code in RETRY_STATUS:
                last_error = ProviderError(source, f"HTTP {resp.status_code}", resp.status_code)
                if not last_attempt:
                    self._backoff(attempt, resp.headers.get("retry-after"), stats)
                continue
            if resp.status_code >= 400:
                self._record_failure(source)
                body = self.redact(resp.text[:200])
                raise ProviderError(source, f"HTTP {resp.status_code}: {body}", resp.status_code)
            try:
                data = resp.json()
            except ValueError as e:
                self._record_failure(source)
                raise ProviderError(source, "response was not JSON") from e
            self._record_success(source)
            if ttl > 0:
                self.kv.set(cache_key, json.dumps(data).encode(), ttl)
            return data

        self._record_failure(source)
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ internals

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        h = {"User-Agent": self.user_agent, "Accept": "application/json"}
        h.update(extra or {})
        for k, v in h.items():
            if k.lower() == "authorization":
                self.register_secret(v.split(" ", 1)[-1])
        return h

    def _cache_key(self, source: str, url: str, params: Mapping[str, Any]) -> str:
        public = {k: v for k, v in sorted(params.items()) if k not in SECRET_PARAMS}
        digest = hashlib.sha256(f"{url}?{urlencode(public)}".encode()).hexdigest()[:32]
        return f"cache:{source}:{digest}"

    def _day(self) -> str:
        return self.clock.now().strftime("%Y%m%d")

    def budget_used(self, source: str) -> int:
        return int(self.kv.get(f"budget:{source}:{self._day()}") or b"0")

    def _spend_budget(self, source: str, spec: SourceSpec) -> None:
        if spec.daily_budget is None:
            return
        used = self.kv.incr(f"budget:{source}:{self._day()}", ttl_s=2 * 86400)
        if used > spec.daily_budget:
            raise BudgetExhausted(source, f"daily budget of {spec.daily_budget} calls used")

    def _space(self, source: str, spec: SourceSpec) -> None:
        now = time.monotonic()
        wait = spec.min_interval_s - (now - self._last_call.get(source, -1e12))
        if wait > 0:
            self.sleep(wait)
        self._last_call[source] = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None, stats: CallStats) -> None:
        stats.retries += 1
        delay = min(60.0, 2.0 ** attempt + random.uniform(0, 0.5))
        if retry_after:
            with contextlib.suppress(ValueError):  # Retry-After may be an HTTP date; keep our delay
                delay = min(60.0, max(delay, float(retry_after)))
        self.sleep(delay)

    def _check_circuit(self, source: str) -> None:
        if self.kv.get(f"circuit:{source}:open") is not None:
            raise CircuitOpen(source, f"too many consecutive failures; paused for {self.breaker_cooldown_s}s")

    def _record_failure(self, source: str) -> None:
        n = self.kv.incr(f"circuit:{source}:fails", ttl_s=self.breaker_cooldown_s)
        if n >= self.breaker_threshold:
            self.kv.set(f"circuit:{source}:open", b"1", self.breaker_cooldown_s)
            self.kv.delete(f"circuit:{source}:fails")

    def _record_success(self, source: str) -> None:
        self.kv.delete(f"circuit:{source}:fails")
