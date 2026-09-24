"""Disabled stubs. They implement the interface so the UI can list them, but
they never make a network call.

Benzinga: the only permitted route found is the official "Basic News API"
free tier via AWS Marketplace (headline, teaser, link). Decision: stub until
after Phase 1.

Investing.com: official RSS feeds exist, but the site terms forbid using or
storing its data without written permission. Decision: stub. No scraping.
"""

from __future__ import annotations

import datetime as dt

from catalystedge.adapters.news.base import NewsAdapter, RawNews


class BenzingaNews(NewsAdapter):
    source_key = "benzinga_news"
    disabled_reason = "disabled stub (official API via AWS Marketplace; revisit after Phase 1)"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        raise AssertionError("unreachable: stub is disabled")


class InvestingRss(NewsAdapter):
    source_key = "investing_rss"
    disabled_reason = "disabled stub (site terms forbid storing data without written permission)"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        raise AssertionError("unreachable: stub is disabled")
