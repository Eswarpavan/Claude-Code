"""Newswire RSS as primary sources: canonical copy, one event per announcement, verification status."""

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from catalystedge.adapters.news.base import RawNews
from catalystedge.adapters.news.rss import RssNews, parse_feed
from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event
from catalystedge.events.common import add_event, ensure_ticker
from catalystedge.ml.sentiment import LexiconSentiment
from catalystedge.pipeline.dedupe import cluster_items
from catalystedge.pipeline.run import persist, process_items
from catalystedge.pipeline.store import seed_sources
from tests.test_event_sources import U

NOW = dt.datetime(2026, 9, 23, 20, 0, tzinfo=dt.UTC)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Acme Robotics Wins $900 Million Multi-Year Contract With U.S. Army</title>
<link>https://www.globenewswire.com/news-release/2026/09/23/1/acme.html</link>
<guid isPermaLink="false">gnw-1</guid><pubDate>Wed, 23 Sep 2026 12:05 GMT</pubDate>
<category>Contracts</category>
<description>SAN JOSE -- Acme Robotics Inc. (NASDAQ: ACME) today announced...</description>
</item>
<item><title>Old release</title><link>https://www.globenewswire.com/x</link><guid>gnw-0</guid>
<pubDate>Mon, 14 Sep 2026 12:00 GMT</pubDate></item></channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Widget Holdings Raises Full-Year Guidance</title><link href="https://www.businesswire.com/news/home/2"/>
<id>bw-2</id><published>2026-09-23T13:00:00Z</published><summary>Widget Holdings Corp (NYSE: WIDG)</summary>
</entry></feed>"""


def test_parse_rss_and_atom_with_exchange_tags():
    [a, old] = parse_feed(RSS)
    assert a.tickers == ("ACME",) and a.published == dt.datetime(2026, 9, 23, 12, 5, tzinfo=dt.UTC)
    assert old.tickers == ()
    [b] = parse_feed(ATOM)
    assert (b.title, b.tickers, b.guid) == ("Widget Holdings Raises Full-Year Guidance", ("WIDG",), "bw-2")
    assert parse_feed("not xml") == []


def test_adapter_keeps_the_window_and_stores_no_body():
    clock = FrozenClock(NOW)
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=RSS)), kv=InMemoryKV(clock),
                      clock=clock, sleep=lambda s: None)
    items = RssNews(http, "globenewswire_rss", "https://www.globenewswire.com/rss", "GlobeNewswire").fetch(
        since=NOW - dt.timedelta(hours=48), now=NOW)
    assert [i.provider_item_id for i in items] == ["gnw-1"] and items[0].provider_tickers == {"ACME": 0.9}
    assert not hasattr(items[0], "body") and "today announced" not in repr(items[0])


def _news(source, headline, minutes, url):
    return RawNews(source, f"{source}-{minutes}", headline, url, NOW - dt.timedelta(hours=6) +
                   dt.timedelta(minutes=minutes), provider_tickers={"ACME": 1.0})


AGG = "Acme Robotics lands $900 million Army contract"
WIRE = "Acme Robotics Wins $900 Million Multi-Year Contract With U.S. Army"


def test_newswire_is_the_canonical_copy_of_a_cluster():
    c = cluster_items([_news("finnhub_news", WIRE, 0, "https://agg.example/a"),
                       _news("globenewswire_rss", WIRE, 5, "https://www.globenewswire.com/a")])
    assert len(c) == 1 and c[0].representative.source_key == "globenewswire_rss" and c[0].is_primary


def _run(db, items):
    ensure_ticker(db, "ACME", U)
    persist(db, process_items(items, U, LexiconSentiment(), NOW), NOW)
    return db.scalars(select(Event).where(Event.symbol == "ACME", Event.event_type == "contract_win")).all()


@pytest.mark.db
def test_aggregator_first_then_newswire_gives_one_verified_event(db):
    seed_sources(db)
    [ev] = _run(db, [_news("finnhub_news", AGG, 0, "https://agg.example/a")])
    assert ev.verification == "unverified"
    evs = _run(db, [_news("globenewswire_rss", WIRE, 30, "https://www.globenewswire.com/a")])
    assert len(evs) == 1 and evs[0].verification == "verified"
    assert evs[0].original_url == "https://www.globenewswire.com/a"


@pytest.mark.db
def test_newswire_first_then_aggregator_adds_nothing(db):
    seed_sources(db)
    [ev] = _run(db, [_news("globenewswire_rss", WIRE, 0, "https://www.globenewswire.com/a")])
    assert ev.verification == "primary" and ev.original_url == "https://www.globenewswire.com/a"
    assert len(_run(db, [_news("finnhub_news", AGG, 45, "https://agg.example/b")])) == 1


@pytest.mark.db
def test_aggregator_only_reports_are_unchanged(db):
    seed_sources(db)
    _run(db, [_news("finnhub_news", AGG, 0, "https://agg.example/a")])
    evs = _run(db, [_news("marketaux_news", "Acme Robotics awarded $900 million U.S. Army contract", 60,
                          "https://agg2.example/b")])
    assert len(evs) == 2 and {e.verification for e in evs} == {"unverified"}


@pytest.mark.db
def test_sec_filing_verifies_earlier_aggregator_event(db):
    seed_sources(db)
    [agg] = _run(db, [_news("finnhub_news", AGG, 0, "https://agg.example/a")])
    ev = add_event(db, symbol="ACME", event_type="contract_win", origin="filing", headline="8-K (Item 8.01): " + WIRE,
                   url="https://www.sec.gov/Archives/x-index.htm", source_key="sec_edgar",
                   available_at=NOW - dt.timedelta(hours=5), polarity="positive", reasons=[], materiality=0.8,
                   classifier_version="t", universe=U)
    assert ev.verification == "primary"
    db.refresh(agg)
    assert agg.verification == "verified" and agg.original_url == "https://www.sec.gov/Archives/x-index.htm"


def test_fda_press_feed_is_a_primary_source():
    from catalystedge.adapters.news.registry import build_news_adapters
    from catalystedge.config import Settings
    from catalystedge.sources import SOURCES

    clock = FrozenClock(NOW)
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)), kv=InMemoryKV(clock), clock=clock)
    fda = [a for a in build_news_adapters(Settings(_env_file=None), http) if a.source_key == "fda_press_rss"]
    assert fda and fda[0].enabled and SOURCES["fda_press_rss"].primary and SOURCES["fda_press_rss"].credibility == 1.0

