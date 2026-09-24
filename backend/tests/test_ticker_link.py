"""Step 4: ticker linking."""

import pytest
from sqlalchemy import select

from catalystedge.db.models import NewsItemTicker, TickerLinkLog
from catalystedge.fixtures import load_json
from catalystedge.pipeline.ticker_link import (
    LINK_THRESHOLD,
    Universe,
    clean_name,
    link_tickers,
    make_aliases,
)

U = Universe.from_sec_company_tickers(load_json("reference/company_tickers.json"))


def link(headline, provider=None):
    return link_tickers(headline, provider or {}, U)


@pytest.mark.parametrize(
    ("headline", "provider", "expected"),
    [
        # company names (case matters: the first letter must be a capital)
        ("NVIDIA beats estimates and raises full-year revenue guidance", {}, ["NVDA"]),
        ("Apple upgraded to Buy at Example Securities on services growth", {}, ["AAPL"]),
        ("Vertex Pharmaceuticals pain drug meets primary endpoint", {}, ["VRTX"]),
        ("Lockheed Martin wins $2.1 billion Pentagon contract", {}, ["LMT"]),
        ("First Solar to report results tomorrow", {}, ["FSLR"]),
        # possessives
        ("FDA approves Vertex's new pain drug", {}, ["VRTX"]),
        # two companies: order preserved, first is primary
        ("Abbott to acquire Hologic for $15 billion in cash", {}, ["ABT", "HOLX"]),
        # exchange tags and cashtags
        ("Allstate (NYSE: ALL) beats quarterly profit estimates", {}, ["ALL"]),
        ("Gartner (NYSE: IT) raises annual outlook", {}, ["IT"]),
        ("$ON jumps after ON Semiconductor raises guidance", {}, ["ON"]),
        # bare all-caps ticker
        ("AMD shares climb on data center demand", {}, ["AMD"]),
        # common-word tickers must NOT be linked from ordinary words
        ("All major indexes rise as Treasury yields fall", {}, []),
        ("Now is the time to buy chip stocks, strategist says", {}, []),
        ("It was a quiet day on Wall Street", {}, []),
        # lowercase common noun is not the company
        ("Why an apple a day matters for investors", {}, []),
        # dollar amounts are not cashtags
        ("Company raises $500 million in debt", {}, []),
        # acronyms are not tickers
        ("FDA and SEC officials meet with CEO group", {}, []),
    ],
)
def test_linking_table(headline, provider, expected):
    assert link(headline, provider).symbols == expected


def test_primary_is_first_mentioned_company():
    r = link("Abbott to acquire Hologic for $15 billion in cash")
    assert r.primary == "ABT"
    assert [m.is_primary for m in r.mentions] == [True, False]


def test_share_classes_collapse_to_one_company_and_are_logged():
    r = link("Alphabet announces $70 billion share buyback", {"GOOGL": 0.9, "GOOG": 0.9})
    assert r.symbols == ["GOOGL"]
    assert any(a.reason == "share_class_collapsed" and a.chosen == "GOOGL" for a in r.ambiguities)


def test_provider_tag_alone_links_when_confident():
    r = link("Chipmaker shares jump on upbeat outlook", {"NVDA": 0.95})
    assert r.symbols == ["NVDA"] and r.mentions[0].method == "provider_tag"


def test_weak_provider_tag_is_rejected_and_logged():
    r = link("Chipmaker shares jump on upbeat outlook", {"NVDA": 0.3})
    assert r.symbols == []
    assert r.ambiguities[0].reason == "below_threshold"


def test_common_word_ticker_from_provider_tag_alone_is_rejected():
    r = link("Now is the time to buy chip stocks", {"NOW": 1.0})
    assert r.symbols == []
    assert r.ambiguities[0].reason == "common_word_ticker_needs_tag_or_name"


def test_common_word_ticker_with_company_name_is_accepted():
    r = link("ServiceNow shares fall after downgrade to Neutral", {"NOW": 0.85})
    assert r.symbols == ["NOW"]
    assert r.mentions[0].method == "name+provider_tag"


def test_corroboration_raises_confidence():
    alone = link("Palantir awarded Army contract").mentions[0].confidence
    both = link("Palantir awarded Army contract", {"PLTR": 0.95}).mentions[0].confidence
    assert both > alone >= LINK_THRESHOLD


def test_unknown_provider_ticker_is_logged_not_linked():
    r = link("Chipmaker shares jump", {"ZZZZ": 1.0})
    assert r.symbols == [] and r.ambiguities[0].reason == "unknown_ticker"


def test_longest_alias_wins_over_overlap():
    r = link("ON Semiconductor raises guidance")
    assert r.symbols == ["ON"] and r.mentions[0].method == "name"


def test_alias_building_from_sec_titles():
    assert clean_name("VERTEX PHARMACEUTICALS INC / MA") == "Vertex Pharmaceuticals"
    assert clean_name("DEERE & CO") == "Deere"
    assert "First" not in make_aliases("FSLR", "First Solar, Inc.")   # too generic alone
    assert "Google" in make_aliases("GOOGL", "Alphabet Inc.")


def test_all_fixture_headlines_link_as_expected():
    """Every headline in the offline fixtures, with the provider tags it came with."""
    import datetime as dt

    from catalystedge.adapters.news.registry import build_news_adapters
    from catalystedge.clock import FrozenClock
    from catalystedge.config import Settings
    from catalystedge.core.http import HttpClient
    from catalystedge.core.kv import InMemoryKV
    from catalystedge.fixtures import FixtureTransport, recorded_at
    from catalystedge.pipeline.collect import collect_news

    now = recorded_at()
    clock = FrozenClock(now)
    http = HttpClient(transport=FixtureTransport(), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)
    settings = Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures", TIINGO_NEWS_ENABLED="true")
    items, _ = collect_news(build_news_adapters(settings, http), now, symbols=["AAPL"])
    got = {i.headline: link(i.headline, i.provider_tickers).symbols for i in items}
    assert got["Allstate (NYSE: ALL) beats quarterly profit estimates on lower catastrophe losses"] == ["ALL"]
    assert got["All major indexes rise as Treasury yields fall"] == []
    assert got["Now is the time to buy chip stocks, strategist says"] == []
    assert got["Alphabet announces $70 billion share buyback"] == ["GOOGL"]
    assert got["Abbott to acquire Hologic for $15 billion in cash"] == ["ABT", "HOLX"]
    assert got["CrowdStrike beats estimates and raises annual guidance"] == ["CRWD"]
    assert dt.timedelta(0) <= now - min(i.published_at for i in items) <= dt.timedelta(hours=48)
    unlinked = [h for h, s in got.items() if not s]
    assert sorted(unlinked) == sorted(["All major indexes rise as Treasury yields fall",
                                       "Now is the time to buy chip stocks, strategist says"])


# ----------------------------------------------------------------------------- persistence


@pytest.mark.db
def test_links_and_ambiguities_are_stored(db):
    import datetime as dt

    from sqlalchemy import text

    from catalystedge.pipeline.store import seed_sources, seed_tickers, store_links

    seed_sources(db)
    seed_tickers(db, U)
    nid = db.execute(text(
        "INSERT INTO news_items (source_key, provider_item_id, headline, url, published_at, fetched_at, "
        "available_at, dedupe_hash) VALUES ('alphavantage_news', 'x', 'h', 'u', :t, :t, :t, 1) RETURNING id"),
        {"t": dt.datetime(2026, 9, 24, tzinfo=dt.UTC)}).scalar_one()
    headline = "Alphabet announces $70 billion share buyback"
    r = link(headline, {"GOOGL": 0.9, "GOOG": 0.9, "ZZZZ": 1.0})
    store_links(db, nid, headline, r)
    rows = db.scalars(select(NewsItemTicker)).all()
    assert [(x.symbol, x.is_primary) for x in rows] == [("GOOGL", True)]
    assert rows[0].method == "name+provider_tag"
    reasons = sorted(x.reason for x in db.scalars(select(TickerLinkLog)))
    assert reasons == ["share_class_collapsed", "unknown_ticker"]
