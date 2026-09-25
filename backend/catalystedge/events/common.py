"""Shared helpers for non-news event sources (SEC filings, earnings, FDA, trials).

All of them write to the same `events` table the news pipeline uses, so the signal
engine scores every catalyst the same way (one pipeline, rule 1 "news first" in the
sense that signals only ever come from dated, sourced events).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.db.models import Event, Ticker
from catalystedge.pipeline.ticker_link import Universe


@dataclass
class IngestReport:
    source: str
    fetched: int = 0
    stored: int = 0
    events: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "ok"          # ok | partial | failed | blocked | disabled

    def as_dict(self) -> dict:
        return {"source": self.source, "fetched": self.fetched, "stored": self.stored, "events": self.events,
                "errors": self.errors[:5], "status": self.status}


def ensure_ticker(session: Session, symbol: str, universe: Universe | None = None) -> None:
    if session.get(Ticker, symbol) is not None:
        return
    c = universe.by_symbol.get(symbol) if universe else None
    session.add(Ticker(symbol=symbol, cik=c.cik if c else None, name=c.name if c else symbol,
                       aliases=list(c.aliases) if c else []))
    session.flush()


def cik_map(universe: Universe) -> dict[str, str]:
    return {c.cik: c.symbol for c in universe.by_symbol.values() if c.cik}


def add_event(session: Session, *, symbol: str, event_type: str, origin: str, headline: str, url: str,
              source_key: str, available_at: dt.datetime, polarity: str, reasons: list[str],
              materiality: float, classifier_version: str, strength: str | None = "normal",
              sentiment: dict | None = None, mixed: dict | None = None, accession: str | None = None,
              fda_event_id: int | None = None, earnings_key: dict | None = None, credibility: float = 1.0,
              universe: Universe | None = None) -> Event | None:
    """Insert unless the same (symbol, type, source record) already exists. Returns the new event or None."""
    ensure_ticker(session, symbol, universe)
    q = select(Event.id).where(Event.symbol == symbol, Event.event_type == event_type, Event.origin == origin)
    if accession:
        q = q.where(Event.accession == accession)
    elif fda_event_id:
        q = q.where(Event.fda_event_id == fda_event_id)
    elif earnings_key:
        q = q.where(Event.earnings_key == earnings_key)
    else:
        q = q.where(Event.headline == headline, Event.available_at == available_at)
    if session.scalar(q.limit(1)) is not None:
        return None
    e = Event(symbol=symbol, event_type=event_type, origin=origin, headline=headline, url=url, source_key=source_key,
              available_at=available_at, polarity=polarity, strength=strength if polarity == "positive" else None,
              sentiment=sentiment, mixed_resolution=mixed, materiality=materiality, novelty=1.0,
              credibility=credibility, reasons=reasons, classifier_version=classifier_version, accession=accession,
              fda_event_id=fda_event_id, earnings_key=earnings_key, verification="primary", original_url=url)
    session.add(e)
    session.flush()
    # The same announcement already seen through a news aggregator is now confirmed by this primary source.
    from catalystedge.pipeline.run import is_primary_event, same_catalyst

    for other in same_catalyst(session, symbol, event_type, available_at):
        if other.id != e.id and not is_primary_event(other):
            other.verification, other.original_url = "verified", url
    session.flush()
    return e
