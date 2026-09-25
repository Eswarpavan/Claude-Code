"""Market-reaction check (Tiingo): "the news looks positive, but has the stock already moved too much?"

For each shown signal: catalyst price = the close before the news; current price = Tiingo's latest price
(IEX real-time endpoint, one request for all shown symbols, official API with the user's key; it counts
toward the same 45/hour budget as Tiingo end-of-day prices). Outside market hours the decision-day close is
used and no request is made.

Relative volume is the END-OF-DAY volume vs its 20-session average. Tiingo's free intraday data covers only
the IEX exchange's own volume, so a time-of-day-aware relative volume is not possible honestly on the free
plan; the display says so.

This is a FLAG. It does not change confidence or hide signals: the early-move idea did not pass the backtest
bar (docs/BACKTEST_RAW.md), so it stays informational until it does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from catalystedge.core import calendar
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.paper.engine import catalyst_band

IEX = "https://api.tiingo.com/iex/"
BANDS = {"very_early": "0-5%: pre-ignition / very early", "early": "5-10%: early",
         "borderline": "10-15%: borderline, already moving", "extended": "> 15%: extended",
         "reversed": "below the pre-news price: reaction reversed"}
LIMITATION = ("End-of-day relative volume (vs 20-day average): Tiingo's free intraday data has only IEX exchange "
              "volume, so time-of-day relative volume is not available.")


def market_open(now: dt.datetime) -> bool:
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    return calendar.is_session(day) and calendar.session_open(day) <= now < calendar.session_close(day)


def fetch_quotes(http: HttpClient, api_key: str | None, symbols: Sequence[str]) -> dict[str, dict]:
    """{SYMBOL: {'price', 'as_of'}} from one IEX request. Empty when there is no key or on failure."""
    if not api_key or not symbols:
        return {}
    tickers = ",".join(sorted({s.replace(".", "-").lower() for s in symbols}))
    try:
        rows = http.get_json("tiingo_iex", IEX, {"tickers": tickers, "token": api_key})
    except SourceError:
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        price = r.get("tngoLast") or r.get("last")
        if r.get("ticker") and price:
            out[r["ticker"].upper().replace("-", ".")] = {"price": float(price),
                                                          "as_of": r.get("timestamp") or r.get("lastSaleTimestamp")}
    return out


def check(features: dict, quote: dict | None) -> dict | None:
    p = (features or {}).get("price") or {}
    pre, close = p.get("pre_event_close"), p.get("last_close")
    if not pre:
        return None
    price, source = (quote["price"], "tiingo_iex") if quote else (close, "decision-day close")
    move = (price / pre - 1) * 100 if price else None
    band = catalyst_band(move)
    return {"catalyst_price": round(pre, 4), "price_at_detection": round(close, 4) if close else None,
            "current_price": round(price, 4) if price else None, "price_source": source,
            "as_of": quote.get("as_of") if quote else None,
            "move_since_catalyst_pct": round(move, 2) if move is not None else None,
            "extended_status": band, "extended_label": BANDS.get(band or "", "unknown"),
            "relative_volume": round(p["volume_ratio"], 2) if p.get("volume_ratio") is not None else None,
            "relative_volume_note": LIMITATION, "affects_score": False}


def annotate(signals: Sequence, http: HttpClient, api_key: str | None, now: dt.datetime) -> dict:
    """Attach features['market_check'] to shown signals. One Tiingo request, only while the market is open."""
    live = market_open(now)
    quotes = fetch_quotes(http, api_key, [s.symbol for s in signals]) if live else {}
    done = 0
    for sig in signals:
        mc = check(sig.features or {}, quotes.get(sig.symbol))
        if mc is not None:
            sig.features = {**(sig.features or {}), "market_check": {**mc, "checked_at": now.isoformat()}}
            done += 1
    return {"checked": done, "live_quotes": len(quotes), "market_open": live}
