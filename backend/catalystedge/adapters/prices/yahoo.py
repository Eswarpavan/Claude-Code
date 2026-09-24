"""Yahoo Finance chart endpoint: UNOFFICIAL and FRAGILE (undocumented, rate-limits
without warning). Fallback only. No spoofed browser headers: it declares CatalystEdge
like every other request, and simply fails if Yahoo refuses."""

from __future__ import annotations

import datetime as dt

from catalystedge.adapters.prices.base import Bar, PriceAdapter

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooEOD(PriceAdapter):
    source_key = "yahoo_eod"
    fragile = True

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        p1 = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp())
        p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), dt.UTC).timestamp())
        data = self.http.get_json(self.source_key, URL.format(symbol=symbol.replace(".", "-")),
                                  {"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"})
        result = ((data or {}).get("chart") or {}).get("result") or []
        if not result:
            return []
        r = result[0]
        tz_offset = int((r.get("meta") or {}).get("gmtoffset") or -14400)
        q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        adj = (((r.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
        out = []
        for i, ts in enumerate(r.get("timestamp") or []):
            day = (dt.datetime.fromtimestamp(ts, dt.UTC) + dt.timedelta(seconds=tz_offset)).date()
            vals = [q.get(k, [None])[i] if i < len(q.get(k, [])) else None for k in ("open", "high", "low", "close")]
            if None in vals or not (start <= day <= end):
                continue
            vol = q.get("volume", [0])[i] or 0
            out.append(Bar(symbol, day, *map(float, vals), float(adj[i]) if i < len(adj) and adj[i] else None,
                           int(vol), self.source_key))
        return out
