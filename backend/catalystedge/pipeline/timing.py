"""Point-in-time helpers (hard rule 7: no lookahead)."""

from __future__ import annotations

import datetime as dt

from catalystedge.clock import ensure_utc


def news_available_at(published_at: dt.datetime, fetched_at: dt.datetime) -> dt.datetime:
    """When CatalystEdge could first have acted on a news item.

    Live: never earlier than the moment we actually fetched it, even if the
    provider says it was published earlier. This makes a wrong provider
    timezone harmless for lookahead: it can only make an item look older.
    """
    return max(ensure_utc(published_at), ensure_utc(fetched_at))
