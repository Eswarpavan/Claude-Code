"""The news adapter interface. One file per provider implements it.

Adapters return `RawNews`, which by design carries only what CatalystEdge may
store: headline, publisher, timestamp, link, and the provider's own ticker
tags/scores. There is no field for article bodies or summaries, so an adapter
cannot accidentally persist them.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from catalystedge.clock import ensure_utc
from catalystedge.core.http import HttpClient


@dataclass(frozen=True)
class RawNews:
    source_key: str
    provider_item_id: str
    headline: str
    url: str
    published_at: dt.datetime
    publisher: str | None = None
    # Ticker -> provider confidence/relevance in 0..1 (1.0 when the provider gives no score).
    provider_tickers: dict[str, float] = field(default_factory=dict)
    provider_sentiment: float | None = None   # provider's own score, kept only as a feature

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", ensure_utc(self.published_at))
        object.__setattr__(self, "headline", " ".join(self.headline.split()))
        object.__setattr__(self, "provider_tickers",
                           {t.upper().strip(): float(s) for t, s in self.provider_tickers.items() if t.strip()})


class AdapterDisabled(Exception):
    """The adapter is intentionally off (missing key, disabled stub, or feature flag)."""


class NewsAdapter(ABC):
    source_key: str
    #: Human-readable reason when the adapter is off; None when it can run.
    disabled_reason: str | None = None

    def __init__(self, http: HttpClient):
        self.http = http

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: Sequence[str] = ()) -> list[RawNews]:
        """Fetch items published in [since, now]. Raises AdapterDisabled if off."""
        if self.disabled_reason:
            raise AdapterDisabled(f"{self.source_key}: {self.disabled_reason}")
        return self._fetch(since=ensure_utc(since), now=ensure_utc(now), symbols=list(symbols))

    @abstractmethod
    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]: ...
