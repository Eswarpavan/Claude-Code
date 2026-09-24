"""Scheduler entry point for primary events (called by catalystedge/jobs.py::job_events)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.events.ingest import ingest_all
from catalystedge.ml.sentiment import SentimentModel
from catalystedge.pipeline.ticker_link import Universe
from catalystedge.reference import load_universe


def poll_events(session: Session, http: HttpClient, settings: Settings, now: dt.datetime,
                universe: Universe | None = None, model: SentimentModel | None = None) -> dict:
    """SEC 8-K + Form 4, earnings surprises, openFDA, ClinicalTrials.gov. Returns a status dict
    per source; never raises for a single source failure."""
    if universe is None:
        universe, _ = load_universe(settings, http)
    reports = ingest_all(session, http, settings, universe, now, model)
    return {"sources": [r.as_dict() for r in reports], "new_events": sum(r.events for r in reports),
            "status": "ok" if all(r.status in ("ok", "disabled") for r in reports) else "partial"}
