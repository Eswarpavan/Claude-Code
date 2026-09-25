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
    hourly_budget: int | None = None   # max calls per UTC hour (Tiingo free: 50/h)
    rate_group: str | None = None      # sources sharing one provider key share spacing and budgets
    shared_spacing: bool = False       # limit is per IP/key: spacing shared by every process (core.ratelimit)
    primary: bool = False              # original/authoritative source (SEC, newswire, agency): confirms a catalyst

    @property
    def group(self) -> str:
        return self.rate_group or self.key


SOURCES: dict[str, SourceSpec] = {
    s.key: s
    for s in [
        # Confirmed live: 60 calls/min (x-ratelimit-limit header), non-commercial. We use at most ~50/min.
        SourceSpec("finnhub_news", "news", True, False, 0.7, 1.2, 5000, 240, 300, rate_group="finnhub"),
        # Published: 100 requests/day, 3 articles/request. We spend at most 90/day.
        SourceSpec("marketaux_news", "news", True, False, 0.65, 2.0, 90, 1800, 1800),
        # Published: 25 requests/day shared by all AV functions. News gets 10.
        SourceSpec("alphavantage_news", "news", True, False, 0.65, 15.0, 10, 7200, 7200),
        # Power plan only (free key gets HTTP 403, confirmed live): 10,000/h. Off unless TIINGO_NEWS_ENABLED=true.
        SourceSpec("tiingo_news", "news", True, False, 0.7, 1.0, 1000, 600, 900, enabled_by_default=False),
        # SEC fair-access policy: max 10 requests/s with a declared User-Agent. We use at most ~6/s.
        SourceSpec("sec_edgar", "event", True, False, 1.0, 0.15, None, 86400, 600, rate_group="sec",
                   shared_spacing=True, primary=True),
        # The "latest filings" Atom feed changes every minute: short cache, same SEC rate group.
        SourceSpec("sec_feed", "event", True, False, 1.0, 0.15, None, 240, 600, rate_group="sec",
                   shared_spacing=True),
        # Finnhub earnings calendar (actual vs estimate); shares the 60/min key limit.
        SourceSpec("finnhub_earnings", "event", True, False, 1.0, 1.2, 200, 1800, 3600, rate_group="finnhub"),
        # openFDA: 240/min, 1,000/day per IP without a key. We spend at most 300/day.
        SourceSpec("openfda", "event", True, False, 1.0, 0.5, 300, 3600, 21600, primary=True),
        # ClinicalTrials.gov v2: ~50/min per IP (third-party reported). We stay far below.
        SourceSpec("clinicaltrials", "event", True, False, 0.9, 1.5, 300, 3600, 21600, primary=True),
        # Prices. Tiingo free (confirmed live): 50 req/h, 1,000/day, 500 unique symbols/month. We use 45/h, 900/day.
        SourceSpec("tiingo_eod", "price", True, False, 1.0, 0.5, 900, 3600, 86400, hourly_budget=45),
        # Tiingo IEX real-time quotes: same key, same 45/h budget as EOD (rate group), one request per refresh.
        SourceSpec("tiingo_iex", "price", True, False, 1.0, 0.5, 900, 300, 300, hourly_budget=45,
                   rate_group="tiingo_eod"),
        # Finnhub /quote shares the news endpoint's 60/min key limit.
        SourceSpec("finnhub_quote", "price", True, False, 1.0, 1.2, 5000, 30, 86400, rate_group="finnhub"),
        # Unofficial fallbacks (FRAGILE): no published limits; stay very polite.
        SourceSpec("stooq_eod", "price", False, True, 0.8, 2.0, 200, 3600, 86400),
        # Newswire RSS (public syndication feeds): primary sources for company announcements. No published
        # limits: one request per feed every 5 minutes, >= 2 s apart, cached for 4 minutes.
        SourceSpec("globenewswire_rss", "news", True, False, 0.95, 2.0, 400, 240, 300, primary=True),
        SourceSpec("prnewswire_rss", "news", True, False, 0.95, 2.0, 400, 240, 300, primary=True),
        SourceSpec("businesswire_rss", "news", True, False, 0.95, 2.0, 400, 240, 300, primary=True),
        # FDA press releases RSS (official): confirms approvals; many headlines name only the drug, not the company.
        SourceSpec("fda_press_rss", "news", True, False, 1.0, 5.0, 200, 900, 900, primary=True),
        # Nasdaq Trader halts RSS (official). The feed is small and changes during market hours: cache 60 s,
        # >= 5 s between requests; polled with the event sources.
        SourceSpec("nasdaq_halts", "event", True, False, 1.0, 5.0, 2000, 60, 120, primary=True),
        # Government contracts (official). DoD announces daily ~5 pm ET: cache 30 min. USAspending lags weeks:
        # once every 12 h is plenty. SAM.gov (free api.data.gov key): 1,000 requests/day; we use a handful.
        SourceSpec("dod_contracts", "event", True, False, 1.0, 5.0, 200, 1800, 1800, primary=True),
        SourceSpec("usaspending", "event", True, False, 1.0, 5.0, 50, 43200, 43200, primary=True),
        SourceSpec("sam_gov", "event", True, False, 1.0, 5.0, 100, 21600, 21600, primary=True),
        # FINRA short interest (official API): twice-monthly data, so one request per shown symbol per day.
        SourceSpec("finra", "event", True, False, 1.0, 1.0, 200, 86400, 86400, primary=True),
        SourceSpec("benzinga_news", "news", True, False, 0.75, 1.0, None, 600, 900, enabled_by_default=False,
                   note="disabled stub: revisit after Phase 1"),
        SourceSpec("investing_rss", "news", True, False, 0.5, 60.0, None, 1800, 1800, enabled_by_default=False,
                   note="disabled stub: ToS forbids storing data without written permission"),
    ]
}
