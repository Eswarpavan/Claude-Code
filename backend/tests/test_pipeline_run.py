"""Stage-1 pipeline end to end on fixtures, storage of events, and the sample command."""

import pytest
from sqlalchemy import func, select

from catalystedge.adapters.news.registry import build_news_adapters
from catalystedge.cli import main
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event, NewsItemTicker
from catalystedge.fixtures import FixtureTransport, load_json, recorded_at
from catalystedge.ml.sentiment import LexiconSentiment, SentimentScore
from catalystedge.pipeline.run import persist, process_news
from catalystedge.pipeline.store import seed_sources, seed_tickers
from catalystedge.pipeline.ticker_link import Universe

NOW = recorded_at()
U = Universe.from_sec_company_tickers(load_json("reference/company_tickers.json"))


def run_fixtures():
    clock = FrozenClock(NOW)
    http = HttpClient(transport=FixtureTransport(), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)
    settings = Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures")
    return process_news(build_news_adapters(settings, http), U, LexiconSentiment(), NOW, ["AAPL"])


def test_only_expected_events_are_signal_eligible():
    processed, _ = run_fixtures()
    eligible = sorted((e.symbol, e.event_type) for p in processed for e in p.events if e.is_signal_eligible)
    assert eligible == [
        ("AAPL", "upgrade"), ("ALL", "earnings_beat"), ("HOLX", "m_and_a_target"), ("IT", "guidance_raise"),
        ("LMT", "contract_win"), ("MDGL", "fda_approval"), ("NVDA", "guidance_raise"), ("ON", "guidance_raise"),
        ("PLTR", "contract_win"), ("VRTX", "positive_trial"),
    ]


def test_sentiment_tie_reads_neutral():
    assert SentimentScore(1 / 3, 1 / 3, 1 / 3, "x").label == "neutral"


@pytest.mark.db
def test_persist_stores_events_links_and_is_idempotent(db):
    seed_sources(db)
    seed_tickers(db, U)
    processed, _ = run_fixtures()
    first = persist(db, processed, NOW)
    second = persist(db, processed, NOW)
    assert first == sum(len(p.events) for p in processed) and second == 0
    nvda = db.scalars(select(Event).where(Event.symbol == "NVDA")).one()
    assert nvda.credibility == pytest.approx(0.75)   # finnhub 0.70 + confirmed by marketaux
    assert nvda.novelty == 1.0 and nvda.available_at >= NOW   # never before we fetched it
    assert db.scalar(select(func.count()).select_from(NewsItemTicker)) == 16   # 15 linked stories, ABT+HOLX
    intel = db.scalars(select(Event).where(Event.symbol == "INTC")).one()
    assert intel.polarity == "mixed" and intel.mixed_resolution["conflict"].startswith("guidance cut")


def test_sample_command_on_fixtures(capsys):
    assert main(["sample-news", "--fixtures", "--all"]) == 0
    out = capsys.readouterr().out
    assert "FIXTURES (hand-written sample data, not real news)" in out
    assert "UNCALIBRATED" in out and "DIAGNOSTIC VIEW" in out
    assert "NVDA:guidance_raise/positive" in out and "INTC:earnings_beat/mixed" in out
    assert "benzinga_news      disabled" in out
    assert "fixture-key" not in out
