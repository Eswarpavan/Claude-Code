"""Marketaux news (free tier: 100 requests/day, 3 articles/request).

One call per poll: latest US-market news with entity (ticker) tagging.
"""

from __future__ import annotations

import datetime as dt
from statistics import mean

from catalystedge.adapters.news.base import NewsAdapter, RawNews
from catalystedge.core.http import HttpClient

URL = "https://api.marketaux.com/v1/news/all"


class MarketauxNews(NewsAdapter):
    source_key = "marketaux_news"

    def __init__(self, http: HttpClient, api_key: str | None, per_request: int = 3):
        super().__init__(http)
        self.api_key = api_key
        self.per_request = per_request
        if not api_key:
            self.disabled_reason = "MARKETAUX_API_KEY not set"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        params = {
            "countries": "us", "language": "en", "filter_entities": "true", "must_have_entities": "true",
            "published_after": since.strftime("%Y-%m-%dT%H:%M:%S"), "limit": self.per_request,
            "api_token": self.api_key,
        }
        data = self.http.get_json(self.source_key, URL, params)
        out: list[RawNews] = []
        for row in (data or {}).get("data", []):
            title, url, published = row.get("title"), row.get("url"), row.get("published_at")
            if not title or not url or not published:
                continue
            equities = [e for e in row.get("entities", []) if e.get("type") == "equity" and e.get("symbol")]
            scores = [e["sentiment_score"] for e in equities if isinstance(e.get("sentiment_score"), int | float)]
            out.append(RawNews(
                source_key=self.source_key,
                provider_item_id=str(row.get("uuid") or url),
                headline=title,
                url=url,
                published_at=dt.datetime.fromisoformat(published.replace("Z", "+00:00")),
                publisher=row.get("source"),
                provider_tickers={e["symbol"]: _match_score(e) for e in equities},
                provider_sentiment=mean(scores) if scores else None,
            ))
        return out


def _match_score(entity: dict) -> float:
    """Marketaux match_score is 0-100 in practice; normalise to 0..1 (0.5 if absent)."""
    raw = entity.get("match_score")
    if not isinstance(raw, int | float):
        return 0.5
    return min(1.0, raw / 100 if raw > 1 else float(raw))
