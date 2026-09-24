"""Finnhub /quote: today's official open (used to fill orders at the open) and last price."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from catalystedge.core.http import HttpClient

URL = "https://finnhub.io/api/v1/quote"


@dataclass(frozen=True)
class Quote:
    symbol: str
    open: float | None
    last: float | None
    prev_close: float | None
    as_of: dt.datetime | None


class FinnhubQuote:
    source_key = "finnhub_quote"

    def __init__(self, http: HttpClient, api_key: str | None):
        self.http = http
        self.api_key = api_key

    def quote(self, symbol: str) -> Quote | None:
        if not self.api_key:
            return None
        d = self.http.get_json(self.source_key, URL, {"symbol": symbol, "token": self.api_key}, ttl_s=30)
        if not isinstance(d, dict) or not d.get("t"):
            return None
        pos = lambda k: float(d[k]) if d.get(k) not in (None, 0) else None  # noqa: E731
        return Quote(symbol, pos("o"), pos("c"), pos("pc"), dt.datetime.fromtimestamp(int(d["t"]), dt.UTC))
