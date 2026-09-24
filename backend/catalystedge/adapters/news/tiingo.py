"""Tiingo News (Power plan; off unless TIINGO_NEWS_ENABLED=true)."""

from __future__ import annotations

import datetime as dt

from catalystedge.adapters.news.base import NewsAdapter, RawNews
from catalystedge.core.http import HttpClient

URL = "https://api.tiingo.com/tiingo/news"


class TiingoNews(NewsAdapter):
    source_key = "tiingo_news"

    def __init__(self, http: HttpClient, api_key: str | None, enabled: bool, limit: int = 100):
        super().__init__(http)
        self.api_key = api_key
        self.limit = limit
        if not enabled:
            self.disabled_reason = "TIINGO_NEWS_ENABLED=false (Tiingo News needs the Power plan)"
        elif not api_key:
            self.disabled_reason = "TIINGO_API_KEY not set"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        params = {"startDate": since.date().isoformat(), "limit": self.limit, "sortBy": "publishedDate"}
        if symbols:
            params["tickers"] = ",".join(s.lower() for s in symbols)
        data = self.http.get_json(self.source_key, URL, params, {"Authorization": f"Token {self.api_key}"})
        out: list[RawNews] = []
        for row in data if isinstance(data, list) else []:
            title, url, published = row.get("title"), row.get("url"), row.get("publishedDate")
            if not title or not url or not published:
                continue
            item = RawNews(
                source_key=self.source_key,
                provider_item_id=str(row.get("id") or url),
                headline=title,
                url=url,
                published_at=dt.datetime.fromisoformat(published.replace("Z", "+00:00")),
                publisher=row.get("source"),
                provider_tickers={t: 0.9 for t in row.get("tickers", [])},
            )
            if item.published_at >= since:
                out.append(item)
        return out
