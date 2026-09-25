"""Step 3b: news adapters, collection and dedupe, all against offline fixtures."""

import dataclasses
import datetime as dt

import httpx
import pytest

from catalystedge.adapters.news.alphavantage import AlphaVantageNews
from catalystedge.adapters.news.base import AdapterDisabled, RawNews
from catalystedge.adapters.news.finnhub import FinnhubNews
from catalystedge.adapters.news.marketaux import MarketauxNews
from catalystedge.adapters.news.registry import build_news_adapters
from catalystedge.adapters.news.stubs import BenzingaNews, InvestingRss
from catalystedge.adapters.news.tiingo import TiingoNews
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.fixtures import FixtureTransport, recorded_at
from catalystedge.pipeline.collect import collect_news
from catalystedge.pipeline.dedupe import cluster_items, jaccard, normalize_url, simhash64, tokens

NOW = recorded_at()
SINCE = NOW - dt.timedelta(hours=48)


@pytest.fixture
def transport():
    return FixtureTransport()


@pytest.fixture
def http(transport):
    clock = FrozenClock(NOW)
    return HttpClient(transport=transport, kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def settings(**kw):
    base = dict(CATALYSTEDGE_DATA_MODE="fixtures")
    base.update(kw)
    return Settings(_env_file=None, **base)


# ----------------------------------------------------------------------------- the interface


def test_rawnews_has_no_place_for_article_text():
    names = {f.name for f in dataclasses.fields(RawNews)}
    assert not names & {"summary", "body", "content", "description", "snippet", "text"}


def test_rawnews_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        RawNews("x", "1", "h", "u", dt.datetime(2026, 1, 1))


# ----------------------------------------------------------------------------- per provider


def test_finnhub_parses_general_and_company_news(http, transport):
    items = FinnhubNews(http, "k").fetch(since=SINCE, now=NOW, symbols=["AAPL"])
    by_id = {i.provider_item_id: i for i in items}
    # /company-news tags only the queried symbol: a weak hint that needs a headline mention to link.
    assert "9101" in by_id and by_id["9101"].provider_tickers == {"AAPL": 0.0}
    assert by_id["9005"].provider_tickers.keys() == {"ABT", "HOLX"}
    assert by_id["9001"].published_at.tzinfo is not None
    assert "9008" not in by_id   # 60h old: filtered by `since`
    assert {r.url.path for r in transport.requests} == {"/api/v1/news", "/api/v1/company-news"}


def test_marketaux_uses_entity_scores_and_free_tier_page_size(http, transport):
    items = MarketauxNews(http, "k").fetch(since=SINCE, now=NOW)
    assert len(items) == 3
    nvda = next(i for i in items if i.provider_item_id == "mx-1")
    assert nvda.provider_tickers == {"NVDA": pytest.approx(0.952)}
    assert nvda.provider_sentiment == pytest.approx(0.71)
    assert transport.requests[0].url.params["limit"] == "3"


def test_alphavantage_parses_feed_and_timezone_is_configurable(http):
    utc = AlphaVantageNews(http, "k", tz="UTC").fetch(since=SINCE, now=NOW)
    assert len(utc) == 8
    googl = next(i for i in utc if "buyback" in i.headline)
    assert googl.provider_tickers == {"GOOGL": 0.9, "GOOG": 0.9}
    assert googl.published_at == NOW - dt.timedelta(hours=11)
    eastern = AlphaVantageNews(http, "k", tz="America/New_York").fetch(since=SINCE, now=NOW)
    shift = next(i for i in eastern if "buyback" in i.headline).published_at - googl.published_at
    assert shift == dt.timedelta(hours=4)   # EDT in September


def test_alphavantage_quota_message_becomes_quota_status(http):
    def quota(request):
        return httpx.Response(200, json={"Information": "standard API rate limit is 25 requests per day"})

    clock = FrozenClock(NOW)
    client = HttpClient(transport=httpx.MockTransport(quota), kv=InMemoryKV(clock), clock=clock,
                        sleep=lambda s: None)
    _, reports = collect_news([AlphaVantageNews(client, "k")], NOW)
    assert reports[0].status == "quota" and "25 requests" in reports[0].error


def test_tiingo_off_unless_flag_and_key(http):
    assert not TiingoNews(http, "k", enabled=False).enabled
    assert not TiingoNews(http, None, enabled=True).enabled
    items = TiingoNews(http, "k", enabled=True).fetch(since=SINCE, now=NOW)
    assert items[0].provider_tickers == {"CRWD": 0.9}


def test_missing_key_disables_adapter_with_reason(http):
    a = FinnhubNews(http, None)
    assert a.disabled_reason == "FINNHUB_API_KEY not set"
    with pytest.raises(AdapterDisabled):
        a.fetch(since=SINCE, now=NOW)


def test_stubs_are_disabled_and_make_no_calls(http, transport):
    for stub in (BenzingaNews(http), InvestingRss(http)):
        assert not stub.enabled
        with pytest.raises(AdapterDisabled):
            stub.fetch(since=SINCE, now=NOW)
    assert transport.requests == []


# ----------------------------------------------------------------------------- collection


def test_collect_reports_every_source_and_applies_window(http, transport):
    adapters = build_news_adapters(settings(TIINGO_NEWS_ENABLED="true"), http)
    items, reports = collect_news(adapters, NOW, symbols=["AAPL"])
    status = {r.source_key: r.status for r in reports}
    assert status == {"finnhub_news": "ok", "marketaux_news": "ok", "alphavantage_news": "ok",
                      "tiingo_news": "ok", "benzinga_news": "disabled", "investing_rss": "disabled",
                      # recorded fixtures contain no newswire feeds, so they are off in fixture mode
                      "globenewswire_rss": "disabled", "prnewswire_rss": "disabled", "businesswire_rss": "disabled"}
    fh = next(r for r in reports if r.source_key == "finnhub_news")
    assert fh.in_future == 1          # the item dated 2h after "now"
    assert all(NOW - dt.timedelta(hours=48) <= i.published_at <= NOW for i in items)
    assert not any(i.provider_item_id == "9008" for i in items)


def test_one_broken_source_does_not_stop_the_others(http):
    class Broken(FinnhubNews):
        def _fetch(self, **kw):
            raise KeyError("parser bug")

    items, reports = collect_news([Broken(http, "k"), MarketauxNews(http, "k")], NOW)
    assert [r.status for r in reports] == ["failed", "ok"]
    assert len(items) == 3


def test_live_mode_without_keys_reports_disabled_not_errors(http, monkeypatch):
    for var in ("FINNHUB_API_KEY", "MARKETAUX_API_KEY", "ALPHAVANTAGE_API_KEY", "TIINGO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    adapters = build_news_adapters(Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="live"), http)
    _, reports = collect_news(adapters, NOW)
    keyed = {"finnhub_news", "marketaux_news", "alphavantage_news", "tiingo_news", "benzinga_news", "investing_rss"}
    assert {r.status for r in reports if r.source_key in keyed} == {"disabled"}
    # the newswire feeds need no key, so they are on (here they fail: the test transport has no such feed)
    assert {r.source_key for r in reports} - keyed == {"globenewswire_rss", "prnewswire_rss", "businesswire_rss"}


# ----------------------------------------------------------------------------- dedupe


def test_url_normalisation_strips_tracking():
    assert normalize_url("https://www.Example.com/a/?utm_source=x&id=2") == normalize_url("https://example.com/a?id=2")


def test_same_story_across_providers_is_one_cluster(http):
    items, _ = collect_news(build_news_adapters(settings(), http), NOW, symbols=["AAPL"])
    clusters = cluster_items(items)
    nvda = [c for c in clusters if "NVDA" in c.provider_tickers]
    assert len(nvda) == 1 and nvda[0].sources == ["finnhub_news", "marketaux_news"]
    assert nvda[0].representative.source_key == "finnhub_news"   # earliest report wins
    aapl = [c for c in clusters if "AAPL" in c.provider_tickers]
    assert len(aapl) == 1 and len(aapl[0].members) == 2


def test_different_stories_are_not_merged():
    a = tokens("Intel beats estimates but cuts fourth-quarter guidance")
    b = tokens("Intel beats estimates and raises fourth-quarter guidance")
    assert jaccard(a, b) < 0.75


def test_simhash_fits_postgres_bigint_and_is_stable():
    h = simhash64("NVIDIA beats estimates and raises full-year revenue guidance")
    assert -(2**63) <= h < 2**63
    assert h == simhash64("nvidia beats estimates and raises full-year revenue guidance")
