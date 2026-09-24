"""Builds the news adapters in priority order from settings."""

from __future__ import annotations

from catalystedge.adapters.news.alphavantage import AlphaVantageNews
from catalystedge.adapters.news.base import NewsAdapter
from catalystedge.adapters.news.finnhub import FinnhubNews
from catalystedge.adapters.news.marketaux import MarketauxNews
from catalystedge.adapters.news.stubs import BenzingaNews, InvestingRss
from catalystedge.adapters.news.tiingo import TiingoNews
from catalystedge.config import Settings
from catalystedge.core.http import HttpClient

FIXTURE_KEY = "fixture-key"


def build_news_adapters(settings: Settings, http: HttpClient) -> list[NewsAdapter]:
    fixtures = settings.data_mode == "fixtures"

    def key(value: str | None) -> str | None:
        return FIXTURE_KEY if fixtures else value

    return [
        FinnhubNews(http, key(settings.finnhub_api_key)),
        MarketauxNews(http, key(settings.marketaux_api_key)),
        AlphaVantageNews(http, key(settings.alphavantage_api_key), tz=settings.alphavantage_news_tz),
        TiingoNews(http, key(settings.tiingo_api_key), enabled=settings.tiingo_news_enabled),
        BenzingaNews(http),
        InvestingRss(http),
    ]
