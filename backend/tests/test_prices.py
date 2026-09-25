"""Trading calendar, price adapters, fallback chain and point-in-time price storage."""

import datetime as dt
import json

import httpx
import pytest

from catalystedge.adapters.prices.finnhub_quote import FinnhubQuote
from catalystedge.adapters.prices.stooq import StooqEOD
from catalystedge.adapters.prices.tiingo import MONTHLY_SYMBOL_CAP, TiingoEOD
from catalystedge.clock import FrozenClock
from catalystedge.core import calendar
from catalystedge.core.http import BudgetExhausted, HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.prices import PriceService, bar_on, load_bars, store_bars

UTC = dt.UTC
D = dt.date
# Thursday 2026-09-24, 22:00 UTC = 18:00 ET: that day's bar is available.
AFTER_CLOSE = dt.datetime(2026, 9, 24, 22, 0, tzinfo=UTC)
# Same day 15:00 ET: the bar for 9/24 is NOT available yet.
INTRADAY = dt.datetime(2026, 9, 24, 19, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------- calendar


def test_next_session_skips_weekends_and_holidays():
    assert calendar.next_session(D(2026, 9, 25)) == D(2026, 9, 28)          # Fri -> Mon
    assert calendar.next_session(D(2026, 11, 25)) == D(2026, 11, 27)        # Thanksgiving
    assert calendar.next_session(D(2026, 12, 24)) == D(2026, 12, 28)        # Christmas, then weekend
    assert calendar.next_session(D(2026, 9, 26)) == D(2026, 9, 28)          # from a Saturday


def test_early_close_changes_eod_availability():
    assert calendar.session_close(D(2026, 11, 27)).hour == 18               # 13:00 ET early close
    assert calendar.eod_available_at(D(2026, 11, 27)) == dt.datetime(2026, 11, 27, 18, 30, tzinfo=UTC)


def test_last_completed_session_respects_close_time():
    assert calendar.last_completed_session(INTRADAY) == D(2026, 9, 23)
    assert calendar.last_completed_session(AFTER_CLOSE) == D(2026, 9, 24)
    assert calendar.last_completed_session(dt.datetime(2026, 9, 27, 12, tzinfo=UTC)) == D(2026, 9, 25)  # Sunday


def test_add_sessions_counts_trading_days():
    assert calendar.add_sessions(D(2026, 9, 24), 10) == D(2026, 10, 8)
    assert len(calendar.sessions_between(D(2026, 9, 21), D(2026, 9, 25))) == 5


def test_calendar_covers_backtest_history():
    assert calendar.is_session(D(2004, 3, 1))


# ----------------------------------------------------------------------------- adapters


def tiingo_rows(days):
    return [{"date": f"{d.isoformat()}T00:00:00.000Z", "open": 10, "high": 11, "low": 9.5, "close": 10.5,
             "volume": 1000, "adjClose": 10.4} for d in days]


def client(handler, now=AFTER_CLOSE):
    clock = FrozenClock(now)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock,
                      sleep=lambda s: None)


def test_tiingo_parses_and_uses_dash_for_share_classes():
    seen = []

    def h(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=tiingo_rows([D(2026, 9, 23), D(2026, 9, 24)]))

    bars = TiingoEOD(client(h), "k").daily("BRK.B", D(2026, 9, 23), D(2026, 9, 24))
    assert seen == ["/tiingo/daily/brk-b/prices"]
    assert [b.date for b in bars] == [D(2026, 9, 23), D(2026, 9, 24)] and bars[0].adj_close == 10.4


def test_tiingo_monthly_unique_symbol_cap():
    http = client(lambda req: httpx.Response(200, json=[]))
    t = TiingoEOD(http, "k")
    http.kv.set(f"tiingo:symcount:{AFTER_CLOSE:%Y%m}", str(MONTHLY_SYMBOL_CAP).encode(), 999)
    with pytest.raises(BudgetExhausted, match="unique symbols"):
        t.daily("NEWSYM", D(2026, 9, 1), D(2026, 9, 2))


def test_tiingo_repeat_symbol_does_not_count_twice():
    http = client(lambda req: httpx.Response(200, json=[]))
    t = TiingoEOD(http, "k")
    t.daily("AAPL", D(2026, 9, 1), D(2026, 9, 2))
    t.daily("AAPL", D(2026, 9, 3), D(2026, 9, 4))
    assert t.symbols_used_this_month() == 1


def test_tiingo_hourly_budget():
    http = client(lambda req: httpx.Response(200, json=[]))
    t = TiingoEOD(http, "k")
    for i in range(45):
        t.daily("AAPL", D(2026, 1, 1) + dt.timedelta(days=i), D(2026, 1, 1) + dt.timedelta(days=i))
    with pytest.raises(BudgetExhausted, match="hourly"):
        t.daily("AAPL", D(2026, 3, 1), D(2026, 3, 2))


def test_price_adapters_declare_themselves_no_browser_spoofing():
    seen = []

    def h(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(429)

    svc = PriceService([TiingoEOD(client(h), "k")], FrozenClock(AFTER_CLOSE))
    svc.daily("AAPL", D(2026, 9, 24), D(2026, 9, 24))
    assert seen and all(ua.startswith("CatalystEdge/") and "Mozilla" not in ua for ua in seen)


def test_no_unofficial_yahoo_source():
    """Yahoo's chart endpoint is unofficial and its terms do not allow automated use: it must stay out."""
    from catalystedge.config import Settings
    from catalystedge.prices import build_price_adapters
    from catalystedge.sources import SOURCES

    assert "yahoo_eod" not in SOURCES
    keys = [a.source_key for a in build_price_adapters(Settings(_env_file=None), client(lambda r: httpx.Response(500)))]
    assert keys == ["tiingo_eod", "stooq_eod"]


def test_stooq_parses_csv_and_needs_key():
    csv_text = "Date,Open,High,Low,Close,Volume\n2026-09-24,10,11,9,10.5,700\n"
    s = StooqEOD(client(lambda req: httpx.Response(200, text=csv_text)), "k")
    assert s.daily("AAPL", D(2026, 9, 24), D(2026, 9, 24))[0].volume == 700
    assert not StooqEOD(client(lambda r: httpx.Response(200)), None).enabled


def test_finnhub_quote_open():
    body = {"o": 101.5, "c": 102.0, "pc": 100.0, "t": int(AFTER_CLOSE.timestamp())}
    q = FinnhubQuote(client(lambda req: httpx.Response(200, json=body)), "k").quote("AAPL")
    assert q.open == 101.5 and q.prev_close == 100.0
    assert FinnhubQuote(client(lambda req: httpx.Response(200, json={"t": 0})), "k").quote("ZZZ") is None


# ----------------------------------------------------------------------------- service


class Fixed:
    def __init__(self, key, bars=None, error=None, enabled=True):
        self.source_key, self._bars, self._error = key, bars or [], error
        self.enabled, self.disabled_reason = enabled, None if enabled else "off"
        self.http = client(lambda r: httpx.Response(200))

    def daily(self, symbol, start, end):
        if self._error:
            raise self._error
        return self._bars


def bar(day, **kw):
    from catalystedge.adapters.prices.base import Bar

    base = dict(symbol="AAPL", date=day, open=10.0, high=11.0, low=9.0, close=10.5, adj_close=None, volume=100,
                source="x")
    base.update(kw)
    return Bar(**base)


def test_fallback_chain_uses_next_source_on_failure():
    primary = Fixed("tiingo_eod", error=BudgetExhausted("tiingo_eod", "hourly budget"))
    backup = Fixed("stooq_eod", bars=[bar(D(2026, 9, 24))])
    r = PriceService([primary, backup], FrozenClock(AFTER_CLOSE)).daily("AAPL", D(2026, 9, 24), D(2026, 9, 24))
    assert r.source == "stooq_eod" and "hourly" in r.errors["tiingo_eod"]


def test_insane_and_non_session_bars_are_rejected():
    bars = [bar(D(2026, 9, 23)), bar(D(2026, 9, 24), high=8.0), bar(D(2026, 9, 26))]   # bad high; Saturday
    r = PriceService([Fixed("tiingo_eod", bars=bars)], FrozenClock(AFTER_CLOSE)).daily(
        "AAPL", D(2026, 9, 20), D(2026, 9, 30))
    assert [b.date for b in r.bars] == [D(2026, 9, 23)] and r.rejected == 2


def test_no_bar_for_an_unfinished_session():
    bars = [bar(D(2026, 9, 23)), bar(D(2026, 9, 24))]
    r = PriceService([Fixed("tiingo_eod", bars=bars)], FrozenClock(INTRADAY)).daily(
        "AAPL", D(2026, 9, 20), D(2026, 9, 24))
    assert [b.date for b in r.bars] == [D(2026, 9, 23)]


@pytest.mark.db
def test_point_in_time_reads(db):
    store_bars(db, [bar(D(2026, 9, 23)), bar(D(2026, 9, 24), close=12.0)], fetched_at=AFTER_CLOSE)
    # 15:00 ET on 9/24: the 9/24 bar exists in the DB but was not knowable yet.
    assert [b.date for b in load_bars(db, "AAPL", INTRADAY)] == [D(2026, 9, 23)]
    assert [b.date for b in load_bars(db, "AAPL", AFTER_CLOSE)] == [D(2026, 9, 23), D(2026, 9, 24)]
    assert float(bar_on(db, "AAPL", D(2026, 9, 24)).close) == 12.0


@pytest.mark.db
def test_store_is_upsert(db):
    store_bars(db, [bar(D(2026, 9, 24), close=10.5)], AFTER_CLOSE)
    store_bars(db, [bar(D(2026, 9, 24), close=10.6)], AFTER_CLOSE)
    assert float(bar_on(db, "AAPL", D(2026, 9, 24)).close) == 10.6


def test_fixture_json_roundtrip_is_plain_data():
    assert json.loads(json.dumps(tiingo_rows([D(2026, 9, 24)])))[0]["close"] == 10.5
