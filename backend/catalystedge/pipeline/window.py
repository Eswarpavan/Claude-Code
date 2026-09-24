"""Hard rule 4: only the last 48 hours of news exist for scoring.

This module is the single place the window is defined. It is applied
  (a) at ingest: older items are dropped before they are stored,
  (b) at scoring: `assert_fresh` refuses to score anything outside the window,
  (c) by the retention job: `purge_expired_news` deletes rows that aged out.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from catalystedge.clock import ensure_utc

NEWS_WINDOW = dt.timedelta(hours=48)
# Provider clocks drift a little; anything further in the future is bad data.
MAX_FUTURE_SKEW = dt.timedelta(minutes=5)

class StaleNewsError(ValueError):
    """Raised when code tries to score a news item outside the 48-hour window."""


def cutoff(now: dt.datetime) -> dt.datetime:
    return ensure_utc(now) - NEWS_WINDOW


def in_window(published_at: dt.datetime, now: dt.datetime) -> bool:
    """True if cutoff <= published_at <= now + skew. The 48 h boundary is inclusive."""
    published_at, now = ensure_utc(published_at), ensure_utc(now)
    return now - NEWS_WINDOW <= published_at <= now + MAX_FUTURE_SKEW


def assert_fresh(published_at: dt.datetime, now: dt.datetime) -> None:
    if not in_window(published_at, now):
        raise StaleNewsError(f"published_at {published_at.isoformat()} is outside the 48h window at {now.isoformat()}")


@dataclass
class WindowResult[T]:
    kept: list[T]
    too_old: list[T]
    in_future: list[T]


def filter_window[T](items: Iterable[T], now: dt.datetime, published: Callable[[T], dt.datetime]) -> WindowResult[T]:
    now = ensure_utc(now)
    kept: list[T] = []
    too_old: list[T] = []
    in_future: list[T] = []
    for item in items:
        p = ensure_utc(published(item))
        if p < now - NEWS_WINDOW:
            too_old.append(item)
        elif p > now + MAX_FUTURE_SKEW:
            in_future.append(item)
        else:
            kept.append(item)
    return WindowResult(kept, too_old, in_future)


def purge_expired_news(session: Session, now: dt.datetime) -> int:
    """Delete news rows older than the window. Link rows cascade; events keep
    their own headline/url copy and have news_item_id set to NULL."""
    from catalystedge.db.models import NewsItem

    result = session.execute(delete(NewsItem).where(NewsItem.published_at < cutoff(now)))
    return result.rowcount or 0
