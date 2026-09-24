"""Sentiment scoring entry point. Enforces rule 4 at scoring time."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from catalystedge.ml.sentiment import SentimentModel, SentimentScore
from catalystedge.pipeline.window import assert_fresh


def score_headlines(model: SentimentModel, items: Sequence[tuple[str, dt.datetime]], now: dt.datetime
                    ) -> list[SentimentScore]:
    """items: (headline, published_at). Raises StaleNewsError for anything outside the 48 h window."""
    for _, published_at in items:
        assert_fresh(published_at, now)
    return model.predict([headline for headline, _ in items]) if items else []
