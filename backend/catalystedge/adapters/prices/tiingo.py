"""Tiingo end-of-day prices (primary). Free plan, confirmed live: 50 req/h,
1,000 req/day, 500 unique symbols/month; includes delisted tickers."""

from __future__ import annotations

import datetime as dt

from catalystedge.adapters.prices.base import Bar, PriceAdapter
from catalystedge.core.http import BudgetExhausted, HttpClient

BASE = "https://api.tiingo.com/tiingo/daily"
MONTHLY_SYMBOL_CAP = 480   # published 500; keep headroom


class TiingoEOD(PriceAdapter):
    source_key = "tiingo_eod"

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key
        if not api_key:
            self.disabled_reason = "TIINGO_API_KEY not set"

    def _check_symbol_cap(self, symbol: str) -> None:
        month = self.http.clock.now().strftime("%Y%m")
        seen_key = f"tiingo:sym:{month}:{symbol}"
        if self.http.kv.get(seen_key) is not None:
            return
        count = int(self.http.kv.get(f"tiingo:symcount:{month}") or b"0")
        if count >= MONTHLY_SYMBOL_CAP:
            raise BudgetExhausted(self.source_key, f"{MONTHLY_SYMBOL_CAP} unique symbols used this month")
        self.http.kv.incr(f"tiingo:symcount:{month}", ttl_s=40 * 86400)
        self.http.kv.set(seen_key, b"1", 40 * 86400)

    def symbols_used_this_month(self) -> int:
        return int(self.http.kv.get(f"tiingo:symcount:{self.http.clock.now().strftime('%Y%m')}") or b"0")

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        self._check_symbol_cap(symbol)
        ticker = symbol.replace(".", "-").lower()
        rows = self.http.get_json(self.source_key, f"{BASE}/{ticker}/prices",
                                  {"startDate": start.isoformat(), "endDate": end.isoformat()},
                                  {"Authorization": f"Token {self.api_key}"})
        out = []
        for r in rows if isinstance(rows, list) else []:
            day = dt.date.fromisoformat(str(r["date"])[:10])
            if start <= day <= end:
                out.append(Bar(symbol, day, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                               float(r["adjClose"]) if r.get("adjClose") is not None else None,
                               int(r.get("volume") or 0), self.source_key))
        return sorted(out, key=lambda b: b.date)
