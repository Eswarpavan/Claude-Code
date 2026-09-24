"""Scheduled jobs and the on-open refresh, end to end on fixtures and a real Postgres."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from catalystedge import jobs
from catalystedge.adapters.prices.base import Bar
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core import calendar
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import (
    Event,
    Notification,
    PaperOrder,
    PaperPosition,
    RefreshRun,
    Signal,
    Source,
    SourceRun,
    Ticker,
)
from catalystedge.fixtures import FixtureTransport, recorded_at
from catalystedge.ml.sentiment import LexiconSentiment
from catalystedge.paper import engine as paper
from catalystedge.prices import store_bars

pytestmark = pytest.mark.db
UTC = dt.UTC


@pytest.fixture
def clean(engine):
    yield engine
    with engine.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"))]
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


def make_ctx(engine, now, **settings_kw):
    settings = Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures", REFRESH_COOLDOWN_S="300", **settings_kw)
    clock = FrozenClock(now)
    kv = InMemoryKV(clock)
    http = HttpClient(transport=FixtureTransport(), kv=kv, clock=clock, sleep=lambda s: None)
    ctx = jobs.Context(settings, sessionmaker(engine, expire_on_commit=False), http, kv, clock)
    ctx._model = LexiconSentiment()
    return ctx


def test_news_job_stores_events_and_source_health(clean):
    ctx = make_ctx(clean, recorded_at())
    result = jobs.job_news(ctx)
    assert result["new_events"] > 0 and result["sources"]["finnhub_news"] == "ok"
    with ctx.session_factory() as s:
        srcs = {x.key: x for x in s.scalars(select(Source))}
        assert srcs["finnhub_news"].status == "ok" and srcs["finnhub_news"].last_success_at is not None
        assert srcs["benzinga_news"].status == "disabled"
        assert s.scalar(select(Event).where(Event.symbol == "NVDA", Event.polarity == "positive")) is not None
        assert len(s.scalars(select(SourceRun)).all()) == 6
    with ctx.session_factory() as s:
        before = s.scalar(text("SELECT count(*) FROM events"))
    jobs.job_news(ctx)          # the watchlist now adds company-news queries, but nothing is stored twice
    with ctx.session_factory() as s:
        dupes = s.scalar(text("SELECT count(*) FROM (SELECT symbol, headline, event_type FROM events "
                              "GROUP BY 1, 2, 3 HAVING count(*) > 1) d"))
        assert dupes == 0 and s.scalar(text("SELECT count(*) FROM events")) >= before


def test_refresh_cooldown_and_progress(clean):
    ctx = make_ctx(clean, recorded_at())
    rid, started = jobs.start_refresh(ctx, "open")
    assert started
    rid2, started2 = jobs.start_refresh(ctx, "open")       # repeated opens inside the cooldown
    assert (rid2, started2) == (rid, False)
    results = jobs.run_refresh(ctx, rid)
    assert set(results) == set(jobs.REFRESH_STEPS)
    with ctx.session_factory() as s:
        run = s.get(RefreshRun, rid)
        assert run.status == "done" and run.progress_pct == 100.0 and run.finished_at is not None
        assert len(s.scalars(select(SourceRun).where(SourceRun.refresh_id == rid)).all()) >= 6
    ctx.clock.advance(seconds=301)
    assert jobs.start_refresh(ctx, "open")[1] is True


def test_refresh_continues_when_a_step_fails(clean, monkeypatch):
    ctx = make_ctx(clean, recorded_at())

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(jobs, "job_prices", boom)
    rid, _ = jobs.start_refresh(ctx)
    results = jobs.run_refresh(ctx, rid)
    assert "provider exploded" in results["prices"]["error"] and "marked" in results["portfolio"]


def test_concurrent_refresh_is_skipped(clean):
    ctx = make_ctx(clean, recorded_at())
    rid, _ = jobs.start_refresh(ctx)
    with jobs.job_lock(ctx.kv, "refresh"):
        assert jobs.run_refresh(ctx, rid) == {"status": "skipped"}


class OneBarPerDay:
    source_key, enabled, disabled_reason = "tiingo_eod", True, None

    def __init__(self, http):
        self.http, self.calls = http, []

    def daily(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return [Bar(symbol, d, 100, 101, 99, 100.5, None, 1_000_000, "tiingo_eod")
                for d in calendar.sessions_between(start, end)]


def test_prices_job_is_incremental(clean, monkeypatch):
    now = dt.datetime(2026, 9, 24, 22, tzinfo=UTC)
    ctx = make_ctx(clean, now)
    adapter = OneBarPerDay(ctx.http)
    monkeypatch.setattr(jobs, "build_price_adapters", lambda settings, http: [adapter])
    first = jobs.job_prices(ctx, symbols=["SPY"])
    assert first["bars_stored"] > 200
    ctx.clock.advance(days=1)                               # Friday evening
    second = jobs.job_prices(ctx, symbols=["SPY"])
    assert second["bars_stored"] == 1 and adapter.calls[-1][1] == dt.date(2026, 9, 25)
    assert jobs.job_prices(ctx, symbols=["SPY"])["bars_stored"] == 0   # nothing new
    with ctx.session_factory() as s:
        assert float(s.get(Ticker, "SPY").adv20_usd) == pytest.approx(100.5 * 1_000_000)


def _signal(s, symbol="ABC", conf=85.0, day=dt.date(2026, 9, 24)):
    if s.get(Ticker, symbol) is None:
        s.add(Ticker(symbol=symbol, name=symbol, aliases=[], adv20_usd=Decimal("1e9")))
        s.flush()
    sig = Signal(symbol=symbol, as_of_date=day, catalyst_type="contract_win", rule_id="R_CONTRACT", rule_score=conf,
                 confidence=conf, expected_return_pct=3.0, expected_return_basis="prior", holding_days_min=3,
                 holding_days_max=10, entry_ref_price=Decimal("100"), stop_price=Decimal("94"),
                 target_price=Decimal("106"), suggested_size_usd=Decimal("20"), risk_notes=["r"], reason="why",
                 features={}, displayed=True)
    s.add(sig)
    s.flush()
    return sig


def test_eod_then_open_full_cycle_with_email(clean, monkeypatch):
    thu_eve = dt.datetime(2026, 9, 24, 21, 30, tzinfo=UTC)
    ctx = make_ctx(clean, thu_eve, ALERT_EMAIL_TO="me@example.com")
    with ctx.session_factory() as s:
        sig = _signal(s)
        store_bars(s, [Bar("ABC", dt.date(2026, 9, 24), 99, 101, 98, 100, None, 10**6, "t"),
                       Bar("SPY", dt.date(2026, 9, 24), 500, 501, 499, 500, None, 10**7, "t")], thu_eve)
        s.commit()
        sig_id = sig.id
    decided = jobs.job_paper_decide(ctx)
    assert decided["candidates"] == 1 and decided["bought"] == 0          # auto-buy OFF by default
    with ctx.session_factory() as s:
        acct = paper.get_account(s)
        order = paper.manual_buy(s, acct, s.get(Signal, sig_id), thu_eve)
        s.commit()
        assert order.execute_on == dt.date(2026, 9, 25)
    ctx.clock.at = dt.datetime(2026, 9, 25, 13, 35, tzinfo=UTC)          # Friday 09:35 ET, no bar yet
    monkeypatch.setattr(jobs.FinnhubQuote, "quote", lambda self, sym: None)
    assert jobs.job_paper_execute(ctx)["waiting"] == 1
    ctx.clock.at = dt.datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
    with ctx.session_factory() as s:
        store_bars(s, [Bar("ABC", dt.date(2026, 9, 25), 101, 103, 100, 102, None, 10**6, "t")], ctx.clock.now())
        s.commit()
    executed = jobs.job_paper_execute(ctx)
    assert executed["filled"] == 1 and executed["buy_emails_queued"] == 1
    sent = []

    class P:
        name = "fake"

        def send(self, msg):
            sent.append(msg)

    monkeypatch.setattr(jobs, "build_provider", lambda settings: P())
    assert jobs.job_email(ctx)["sent"] == 1 and "paper buy: ABC" in sent[0].subject
    with ctx.session_factory() as s:
        pos = s.scalars(select(PaperPosition)).one()
        assert pos.entry_date == dt.date(2026, 9, 25) and float(pos.why["entry_gap_pct"]) == pytest.approx(1.0)
        assert s.scalars(select(Notification)).one().status == "sent"
        assert s.scalars(select(PaperOrder)).one().status == "filled"


def test_signal_hook_waits_for_engine_module():
    ctx = jobs.Context(Settings(_env_file=None), lambda: None, None, InMemoryKV(), FrozenClock(recorded_at()))
    try:
        import catalystedge.signals.engine  # noqa: F401
    except ImportError:
        assert jobs.job_signals(ctx) == {"status": "signal engine not available yet"}
