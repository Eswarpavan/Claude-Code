"""Nasdaq Trader halts feed: parsed, stored, and shown as a risk flag (never a catalyst)."""

import datetime as dt

import httpx
from sqlalchemy import select

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import TradingHalt
from catalystedge.events.halts import halt_flags, halt_note, ingest_halts, parse_halts

FEED = """<?xml version="1.0"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
<item><title>ACME</title><ndaq:HaltDate>09/23/2026</ndaq:HaltDate><ndaq:HaltTime>10:15:02</ndaq:HaltTime>
<ndaq:IssueSymbol>ACME</ndaq:IssueSymbol><ndaq:IssueName>Acme Robotics Inc</ndaq:IssueName>
<ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
<ndaq:ResumptionDate>09/23/2026</ndaq:ResumptionDate><ndaq:ResumptionTradeTime>10:20:02</ndaq:ResumptionTradeTime>
</item>
<item><title>WIDG</title><ndaq:HaltDate>09/23/2026</ndaq:HaltDate><ndaq:HaltTime>14:00:00</ndaq:HaltTime>
<ndaq:IssueSymbol>WIDG</ndaq:IssueSymbol><ndaq:ReasonCode>T1</ndaq:ReasonCode><ndaq:ResumptionDate></ndaq:ResumptionDate>
</item></channel></rss>"""
NOW = dt.datetime(2026, 9, 23, 21, 0, tzinfo=dt.UTC)


def test_parse_halts_in_eastern_time():
    a, w = parse_halts(FEED)
    assert a["symbol"] == "ACME" and a["halted_at"] == dt.datetime(2026, 9, 23, 14, 15, 2, tzinfo=dt.UTC)
    assert a["resumed_at"] == dt.datetime(2026, 9, 23, 14, 20, 2, tzinfo=dt.UTC) and a["reason_code"] == "LUDP"
    assert w["resumed_at"] is None and parse_halts("<bad") == []


def test_ingest_is_idempotent_and_flags_signals(db):
    clock = FrozenClock(NOW)
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FEED)), kv=InMemoryKV(clock),
                      clock=clock, sleep=lambda s: None)
    assert ingest_halts(db, http).stored == 2
    ingest_halts(db, http)
    assert len(db.scalars(select(TradingHalt)).all()) == 2
    note = halt_note(halt_flags(db, "WIDG", NOW))
    assert "news pending" in note and "still halted" in note
    assert "limit up/limit down" in halt_note(halt_flags(db, "ACME", NOW))
    assert halt_flags(db, "ACME", NOW + dt.timedelta(days=5)) == []
