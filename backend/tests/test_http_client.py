"""Step 3a: the shared HTTP client (rate limits, budgets, cache, retries, breaker, redaction)."""

import datetime as dt

import httpx
import pytest

from catalystedge.clock import FrozenClock
from catalystedge.core.http import BudgetExhausted, CircuitOpen, HttpClient, ProviderError
from catalystedge.core.kv import InMemoryKV
from catalystedge.sources import SourceSpec

NOW = dt.datetime(2026, 9, 24, 13, 30, tzinfo=dt.UTC)
SPEC = SourceSpec("t", "news", True, False, 0.5, min_interval_s=1.0, daily_budget=5, cache_ttl_s=60, poll_every_s=60)


def make(handler, **kw):
    calls = []
    sleeps = []

    def h(request):
        calls.append(request)
        return handler(request, len(calls))

    clock = FrozenClock(NOW)
    client = HttpClient(transport=httpx.MockTransport(h), kv=InMemoryKV(clock), clock=clock,
                        sleep=sleeps.append, specs={"t": kw.pop("spec", SPEC)}, **kw)
    return client, calls, sleeps


def ok(request, n):
    return httpx.Response(200, json={"n": n})


def test_declares_itself_and_never_spoofs_a_browser():
    client, calls, _ = make(ok)
    client.get_json("t", "https://api.example/x")
    ua = calls[0].headers["user-agent"]
    assert ua.startswith("CatalystEdge/") and "Mozilla" not in ua


def test_ttl_cache_avoids_second_call():
    client, calls, _ = make(ok)
    assert client.get_json("t", "https://api.example/x", {"q": 1}) == {"n": 1}
    assert client.get_json("t", "https://api.example/x", {"q": 1}) == {"n": 1}
    assert len(calls) == 1 and client.stats["t"].cache_hits == 1


def test_cache_key_ignores_secret_but_not_other_params():
    client, calls, _ = make(ok)
    client.get_json("t", "https://api.example/x", {"q": 1, "token": "A"})
    client.get_json("t", "https://api.example/x", {"q": 1, "token": "B"})
    client.get_json("t", "https://api.example/x", {"q": 2, "token": "A"})
    assert len(calls) == 2


def test_daily_budget_stops_calls_before_they_happen():
    client, calls, _ = make(ok)
    for i in range(5):
        client.get_json("t", "https://api.example/x", {"i": i})
    with pytest.raises(BudgetExhausted):
        client.get_json("t", "https://api.example/x", {"i": 99})
    assert len(calls) == 5


def test_budget_resets_next_utc_day():
    client, calls, _ = make(ok)
    for i in range(5):
        client.get_json("t", "https://api.example/x", {"i": i})
    client.clock.advance(days=1)
    client.get_json("t", "https://api.example/x", {"i": 100})
    assert len(calls) == 6


def test_retries_429_honouring_retry_after_and_counts_budget_per_attempt():
    def flaky(request, n):
        return httpx.Response(429, headers={"Retry-After": "7"}) if n == 1 else httpx.Response(200, json={"ok": 1})

    client, calls, sleeps = make(flaky)
    assert client.get_json("t", "https://api.example/x") == {"ok": 1}
    assert len(calls) == 2
    assert 7 in [round(s) for s in sleeps]
    assert client.budget_used("t") == 2


def test_no_retry_on_401_and_error_is_redacted():
    def denied(request, n):
        return httpx.Response(401, text=f"invalid key {request.url}")

    client, calls, _ = make(denied)
    with pytest.raises(ProviderError) as e:
        client.get_json("t", "https://api.example/x", {"token": "SUPERSECRET123"})
    assert len(calls) == 1
    assert "SUPERSECRET123" not in str(e.value) and "***" in str(e.value)


def test_authorization_header_value_is_redacted():
    def denied(request, n):
        return httpx.Response(403, text=f"bad token {request.headers['authorization']}")

    client, _, _ = make(denied)
    with pytest.raises(ProviderError) as e:
        client.get_json("t", "https://api.example/x", headers={"Authorization": "Token TKSECRET999"})
    assert "TKSECRET999" not in str(e.value)


def test_network_errors_are_retried_then_reported():
    def boom(request, n):
        raise httpx.ConnectError("proxy said no")

    client, calls, sleeps = make(boom)
    with pytest.raises(ProviderError, match="network error"):
        client.get_json("t", "https://api.example/x")
    assert len(calls) == 3 and len(sleeps) >= 2


def test_circuit_breaker_opens_after_repeated_failures():
    def fail(request, n):
        return httpx.Response(500)

    spec = SourceSpec("t", "news", True, False, 0.5, 0.0, None, 0, 60)
    client, calls, _ = make(fail, spec=spec, breaker_threshold=2, max_attempts=1)
    for _ in range(2):
        with pytest.raises(ProviderError):
            client.get_json("t", "https://api.example/x")
    with pytest.raises(CircuitOpen):
        client.get_json("t", "https://api.example/x")
    assert len(calls) == 2   # the third request never left the process


def test_spacing_between_calls():
    spec = SourceSpec("t", "news", True, False, 0.5, min_interval_s=2.0, daily_budget=None, cache_ttl_s=0,
                      poll_every_s=60)
    client, _, sleeps = make(ok, spec=spec)
    client.get_json("t", "https://api.example/a")
    client.get_json("t", "https://api.example/b")
    assert sleeps and 0 < sleeps[-1] <= 2.0


def test_hourly_quota_429_stops_immediately_and_blocks_the_rest_of_the_hour():
    spec = SourceSpec("t", "price", True, False, 1.0, 0.0, 900, 0, 60, hourly_budget=45)
    client, calls, sleeps = make(lambda req, n: httpx.Response(429), spec=spec)
    with pytest.raises(BudgetExhausted, match="hourly limit"):
        client.get_json("t", "https://api.example/x")
    assert len(calls) == 1 and sleeps == []                    # no retries burning quota
    with pytest.raises(BudgetExhausted, match="hourly budget"):
        client.get_json("t", "https://api.example/y")
    assert len(calls) == 1                                      # nothing else sent this hour
    client.clock.advance(hours=1)
    with pytest.raises(BudgetExhausted):
        client.get_json("t", "https://api.example/z")           # a new hour is tried again (still 429 here)
    assert len(calls) == 2
