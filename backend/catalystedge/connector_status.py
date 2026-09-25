"""Connector states for Source Health, in the user's terms, plus the sources kept manual/reference-only.

  ACTIVE                   last run worked
  RATE LIMITED             stopped by our budget or the provider's limit (tries again next window)
  TEMPORARILY UNAVAILABLE  network error, provider error, or not heard from in a while
  AUTH REQUIRED            needs a key or credentials that are not set (or were refused)
  OFF                      switched off in settings
  MANUAL ONLY              no permitted automated method: open it yourself
  UNSUPPORTED              not built (paid-only or not verifiable)
"""

from __future__ import annotations

AUTH_HINTS = ("not set", "auth required", "http 401", "http 403", "credentials", "api key", "apikey")
RATE_HINTS = ("budget", "http 429", "hourly", "rate limit", "limit is used up")

MANUAL_ONLY = [
    {"name": "Investing.com", "why": "its terms forbid storing its data without written permission"},
    {"name": "Reuters", "why": "no free API or feed (licensed through LSEG only)"},
    {"name": "Yahoo Finance prices", "why": "no official API; the unofficial endpoint was removed"},
    {"name": "Finviz (free site)", "why": "automated collection not permitted; data only via paid Finviz Elite export"},
    {"name": "TradingView data", "why": "no public data API (its embeddable chart widget is fine for viewing)"},
    {"name": "OpenInsider", "why": "no API; the same Form 4 data comes straight from SEC"},
    {"name": "X (Twitter)", "why": "no free read API"},
    {"name": "Google Trends", "why": "no public API (popular libraries scrape it)"},
    {"name": "Nasdaq short-interest pages", "why": "web pages only; FINRA's API is used instead"},
    {"name": "ISM manufacturing/services", "why": "private survey with no free official feed"},
    {"name": "Sherwood News, ACCESS Newswire", "why": "no feed confirmed yet; not collected until one is"},
]
UNSUPPORTED = {"benzinga_news": "AUTH REQUIRED (paid Benzinga API key)",
               "investing_rss": "MANUAL ONLY (terms forbid storing)"}


def connector_state(key: str, health: str, status: str, enabled: bool, last_error: str | None) -> str:
    if key in UNSUPPORTED:
        return UNSUPPORTED[key].split(" (")[0]
    err = (last_error or "").lower()
    if any(h in err for h in AUTH_HINTS):
        return "AUTH REQUIRED"
    if any(h in err for h in RATE_HINTS):
        return "RATE LIMITED"
    if status == "disabled" or not enabled:
        return "OFF"
    if health == "ok":
        return "ACTIVE"
    return "TEMPORARILY UNAVAILABLE"
