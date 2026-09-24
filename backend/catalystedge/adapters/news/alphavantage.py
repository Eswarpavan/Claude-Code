"""Alpha Vantage NEWS_SENTIMENT (free tier: 25 requests/day shared by all functions).

`time_published` is "YYYYMMDDTHHMMSS" with no zone. Alpha Vantage does not
document the zone; ALPHAVANTAGE_NEWS_TZ (default UTC, the conservative
reading) controls it, and the sample command measures the real offset.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from zoneinfo import ZoneInfo

from catalystedge.adapters.news.base import NewsAdapter, RawNews
from catalystedge.core.http import HttpClient, QuotaExceeded

URL = "https://www.alphavantage.co/query"


class AlphaVantageNews(NewsAdapter):
    source_key = "alphavantage_news"

    def __init__(self, http: HttpClient, api_key: str | None, tz: str = "UTC", limit: int = 50):
        super().__init__(http)
        self.api_key = api_key
        self.tz = ZoneInfo(tz)
        self.limit = limit
        if not api_key:
            self.disabled_reason = "ALPHAVANTAGE_API_KEY not set"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        params = {"function": "NEWS_SENTIMENT", "time_from": since.strftime("%Y%m%dT%H%M"), "sort": "LATEST",
                  "limit": self.limit, "apikey": self.api_key}
        if symbols:
            params["tickers"] = ",".join(symbols[:5])
        data = self.http.get_json(self.source_key, URL, params)
        if isinstance(data, dict):
            for key in ("Information", "Note", "Error Message"):
                if key in data:
                    # AV answers HTTP 200 with a message when the daily quota is gone.
                    raise QuotaExceeded(self.source_key, str(data[key])[:160])
        out: list[RawNews] = []
        for row in (data or {}).get("feed", []):
            title, url, ts = row.get("title"), row.get("url"), row.get("time_published")
            if not title or not url or not ts:
                continue
            published = dt.datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=self.tz)
            tickers = {t["ticker"]: float(t.get("relevance_score") or 0)
                       for t in row.get("ticker_sentiment", []) if t.get("ticker")}
            overall = row.get("overall_sentiment_score")
            out.append(RawNews(
                source_key=self.source_key,
                provider_item_id=hashlib.sha1(url.encode()).hexdigest(),
                headline=title,
                url=url,
                published_at=published,
                publisher=row.get("source"),
                provider_tickers=tickers,
                provider_sentiment=float(overall) if overall is not None else None,
            ))
        return out
