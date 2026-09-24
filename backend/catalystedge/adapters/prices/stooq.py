"""Stooq daily CSV: UNOFFICIAL and FRAGILE. Since ~April 2026 an apikey (from a
captcha on stooq.com) is required. Fallback only."""

from __future__ import annotations

import csv
import datetime as dt
import io

from catalystedge.adapters.prices.base import Bar, PriceAdapter
from catalystedge.core.http import HttpClient

URL = "https://stooq.com/q/d/l/"


class StooqEOD(PriceAdapter):
    source_key = "stooq_eod"
    fragile = True

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key
        if not api_key:
            self.disabled_reason = "STOOQ_API_KEY not set (Stooq needs an apikey since ~April 2026)"

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        text = self.http.get_text(self.source_key, URL, {
            "s": f"{symbol.lower().replace('.', '-')}.us", "i": "d", "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"), "apikey": self.api_key,
        })
        if not text.lower().startswith("date,"):
            return []
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            try:
                day = dt.date.fromisoformat(row["Date"])
                bar = Bar(symbol, day, float(row["Open"]), float(row["High"]), float(row["Low"]),
                          float(row["Close"]), None, int(float(row.get("Volume") or 0)), self.source_key)
            except (KeyError, ValueError):
                continue
            if start <= day <= end:
                out.append(bar)
        return out
