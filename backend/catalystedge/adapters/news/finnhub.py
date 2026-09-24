"""Finnhub news (free tier: ~60 calls/min, company news ~1 year, non-commercial).

Two endpoints:
  /news?category=general           market-wide headlines (discovery), 1 call
  /company-news?symbol=X&from&to   per-symbol headlines for the watchlist
"""

from __future__ import annotations

import datetime as dt

from catalystedge.adapters.news.base import NewsAdapter, RawNews
from catalystedge.core.http import HttpClient

BASE = "https://finnhub.io/api/v1"
# Company-news tags link a ticker only together with a mention in the headline
# (ticker, cashtag or name): 0.60 alone is below the linker's 0.80 threshold.
COMPANY_NEWS_TAG_SCORE = 0.0


class FinnhubNews(NewsAdapter):
    source_key = "finnhub_news"

    def __init__(self, http: HttpClient, api_key: str | None, max_symbols: int = 20):
        super().__init__(http)
        self.api_key = api_key
        self.max_symbols = max_symbols
        if not api_key:
            self.disabled_reason = "FINNHUB_API_KEY not set"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        items = self._parse(self.http.get_json(self.source_key, f"{BASE}/news",
                                               {"category": "general", "token": self.api_key}))
        for symbol in symbols[: self.max_symbols]:
            data = self.http.get_json(self.source_key, f"{BASE}/company-news", {
                "symbol": symbol, "from": since.date().isoformat(), "to": now.date().isoformat(),
                "token": self.api_key,
            })
            items += self._parse(data, fallback_symbol=symbol, query_tag_score=COMPANY_NEWS_TAG_SCORE)
        return [i for i in items if since <= i.published_at]

    def _parse(self, data: object, fallback_symbol: str | None = None, query_tag_score: float = 0.9
               ) -> list[RawNews]:
        out: list[RawNews] = []
        for row in data if isinstance(data, list) else []:
            headline, url, ts = row.get("headline"), row.get("url"), row.get("datetime")
            if not headline or not url or not ts:
                continue
            related = [s for s in str(row.get("related") or "").split(",") if s.strip()]
            if not related and fallback_symbol:
                related = [fallback_symbol]
            out.append(RawNews(
                source_key=self.source_key,
                provider_item_id=str(row.get("id") or url),
                headline=headline,
                url=url,
                published_at=dt.datetime.fromtimestamp(int(ts), dt.UTC),
                publisher=row.get("source"),
                # Finnhub's `related` has no score. On /company-news it is just the symbol we asked
                # about, and many of those articles are only loosely related, so it is a weak hint.
                provider_tickers={s: query_tag_score for s in related},
            ))
        return out
