"""Macro context: Fed/BLS feeds and the FRED release calendar, stored as context (never catalysts)."""

import datetime as dt

import httpx
from sqlalchemy import func, select

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event, MacroRelease
from catalystedge.events.macro import ingest_fed_bls, ingest_fred_calendar, macro_context

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
FED = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260923a.htm</link>
<pubDate>Wed, 23 Sep 2026 18:00:00 GMT</pubDate></item></channel></rss>"""
BLS = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Consumer Price Index - August 2026</title>
<link>https://www.bls.gov/news.release/cpi.nr0.htm</link><pubDate>Wed, 23 Sep 2026 12:30:00 GMT</pubDate></item>
</channel></rss>"""
FRED = {"release_dates": [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-09-25"},
                          {"release_id": 999, "release_name": "Obscure", "date": "2026-09-25"},
                          {"release_id": 101, "release_name": "FOMC Press Release", "date": "2026-09-30"}]}


def client(handler):
    clock = FrozenClock(NOW)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def route(req):
    u = str(req.url)
    if "federalreserve" in u:
        return httpx.Response(200, text=FED)
    if "bls.gov" in u:
        return httpx.Response(200, text=BLS)
    return httpx.Response(200, json=FRED)


def test_feeds_and_calendar_become_context_not_events(db):
    ingest_fed_bls(db, client(route), NOW)
    rep = ingest_fred_calendar(db, client(route), "FREDKEY", NOW)
    assert rep.fetched == 2                                               # the obscure release is ignored
    ctx = macro_context(db, NOW)
    assert {r["title"] for r in ctx["recent"]} == {"Federal Reserve issues FOMC statement",
                                                    "Consumer Price Index - August 2026"}
    up = ctx["upcoming"]
    assert [u["title"] for u in up] == ["CPI", "FOMC statement"]
    assert up[0]["at"] == "2026-09-25T12:30:00+00:00" and "approximate" in ctx["note"]
    assert db.scalar(select(func.count()).select_from(Event)) == 0         # never a stock catalyst
    ingest_fed_bls(db, client(route), NOW)
    assert db.scalar(select(func.count()).select_from(MacroRelease)) == 4  # idempotent


def test_fred_needs_a_key_and_hides_it():
    assert ingest_fred_calendar(None, client(route), None, NOW).status == "disabled"
    rep = ingest_fred_calendar(None, client(lambda r: httpx.Response(400, text=str(r.url))), "FREDKEY", NOW)
    assert rep.status == "failed" and "FREDKEY" not in " ".join(rep.errors)
