"""Tiingo market-reaction check: move since the catalyst, bands, one request, flag only."""

import datetime as dt
from types import SimpleNamespace

import httpx

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.signals.market_check import annotate, check, fetch_quotes, market_open

OPEN = dt.datetime(2026, 9, 24, 15, 0, tzinfo=dt.UTC)      # Thursday 11:00 ET
CLOSED = dt.datetime(2026, 9, 24, 22, 0, tzinfo=dt.UTC)
FEATS = {"price": {"pre_event_close": 100.0, "last_close": 104.0, "volume_ratio": 2.4}}


def client(handler, now=OPEN):
    clock = FrozenClock(now)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def test_bands_and_labels():
    assert check(FEATS, None)["extended_status"] == "very_early"                  # decision close: +4%
    c = check(FEATS, {"price": 118.0, "as_of": "t"})
    assert c["move_since_catalyst_pct"] == 18.0 and c["extended_status"] == "extended"
    assert c["price_source"] == "tiingo_iex" and c["affects_score"] is False
    assert "IEX" in c["relative_volume_note"] and c["relative_volume"] == 2.4
    assert check(FEATS, {"price": 97.0, "as_of": "t"})["extended_status"] == "reversed"
    assert check({"price": {}}, None) is None


def test_one_request_for_all_symbols_and_only_while_open():
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=[{"ticker": "ACME", "tngoLast": 108.0, "timestamp": "2026-09-24T15:00:00Z"},
                                         {"ticker": "BRK-B", "last": 500.0}])

    sigs = [SimpleNamespace(symbol="ACME", features=dict(FEATS)), SimpleNamespace(symbol="BRK.B", features={})]
    out = annotate(sigs, client(h), "KEY", OPEN)
    assert len(seen) == 1 and "tickers=acme%2Cbrk-b" in seen[0] or "tickers=acme,brk-b" in seen[0]
    assert out == {"checked": 1, "live_quotes": 2, "market_open": True}
    assert sigs[0].features["market_check"]["move_since_catalyst_pct"] == 8.0
    seen.clear()
    annotate(sigs, client(h, CLOSED), "KEY", CLOSED)
    assert seen == [] and sigs[0].features["market_check"]["price_source"] == "decision-day close"


def test_no_key_or_failure_means_no_quotes_and_no_leak():
    assert fetch_quotes(client(lambda r: httpx.Response(500)), None, ["ACME"]) == {}
    assert fetch_quotes(client(lambda r: httpx.Response(403, text=str(r.url))), "SECRETKEY", ["ACME"]) == {}
    assert market_open(OPEN) and not market_open(CLOSED)
