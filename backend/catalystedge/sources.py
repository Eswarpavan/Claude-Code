"""Per-source limits and polling settings.

These numbers come from docs/ARCHITECTURE.md §3 (free tiers, verified
2026-09-24 via web search). They are deliberately set BELOW the published
limits to leave headroom. When scripts/verify_sources.py reports the real
limits for your keys, this table is updated to match.

Live check 2026-09-24: Finnhub's 60 calls/min (header) and Tiingo News being
off the free plan (HTTP 403) were confirmed, so no number here had to change.
Marketaux and Alpha Vantage are still unverified (no keys yet).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    key: str
    kind: str                      # news | event | price | macro | email
    official: bool
    fragile: bool
    credibility: float             # 0..1 prior used in event scoring
    min_interval_s: float          # spacing between calls (rate limit)
    daily_budget: int | None       # max calls per UTC day that CatalystEdge will spend
    cache_ttl_s: int               # identical requests within this window hit the cache
    poll_every_s: int              # how often the scheduler polls (local profile)
    enabled_by_default: bool = True
    note: str = ""


SOURCES: dict[str, SourceSpec] = {
    s.key: s
    for s in [
        # Confirmed live: 60 calls/min (x-ratelimit-limit header), non-commercial. We use at most ~50/min.
        SourceSpec("finnhub_news", "news", True, False, 0.7, 1.2, 5000, 240, 300),
        # Published: 100 requests/day, 3 articles/request. We spend at most 90/day.
        SourceSpec("marketaux_news", "news", True, False, 0.65, 2.0, 90, 1800, 1800),
        # Published: 25 requests/day shared by all AV functions. News gets 10.
        SourceSpec("alphavantage_news", "news", True, False, 0.65, 15.0, 10, 7200, 7200),
        # Power plan only (free key gets HTTP 403, confirmed live): 10,000/h. Off unless TIINGO_NEWS_ENABLED=true.
        SourceSpec("tiingo_news", "news", True, False, 0.7, 1.0, 1000, 600, 900, enabled_by_default=False),
        # SEC fair-access policy: max 10 requests/s with a declared User-Agent. We use at most ~6/s.
        SourceSpec("sec_edgar", "event", True, False, 1.0, 0.15, None, 86400, 600),
        SourceSpec("benzinga_news", "news", True, False, 0.75, 1.0, None, 600, 900, enabled_by_default=False,
                   note="disabled stub: revisit after Phase 1"),
        SourceSpec("investing_rss", "news", True, False, 0.5, 60.0, None, 1800, 1800, enabled_by_default=False,
                   note="disabled stub: ToS forbids storing data without written permission"),
    ]
}
