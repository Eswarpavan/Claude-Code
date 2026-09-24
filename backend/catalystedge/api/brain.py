"""API for the signal "brain": TimesFM toggle, evidence, catalyst hit rates, backtest report.

Mounted by api/main.py (same login and database session as every other route).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.db.models import ModelRegistryRow, Signal, Ticker
from catalystedge.ml import timeseries as tsm
from catalystedge.signals.report import catalyst_report, evidence, latest_backtest


class TimesFMBody(BaseModel):
    enabled: bool
    mode: Literal["feature", "filter"] | None = None


def _queue_timesfm_refresh() -> str:
    """Celery worker when configured, else a background thread. Never blocks the request."""
    from catalystedge.api import main

    if main.state.settings.celery_broker_url or main.state.settings.redis_url:
        try:
            from catalystedge.worker.celery_app import timesfm_refresh

            timesfm_refresh.delay()
            return "queued"
        except Exception:
            pass
    from catalystedge import jobs

    ctx = main._ctx()
    threading.Thread(target=lambda: (jobs.job_timesfm(ctx), jobs.job_signals(ctx)), daemon=True).start()
    return "started"


def _rescore_in_background() -> str:
    """Re-score today's signals without TimesFM (after switching it off)."""
    from catalystedge import jobs
    from catalystedge.api import main

    ctx = main._ctx()
    threading.Thread(target=lambda: jobs.job_signals(ctx), daemon=True).start()
    return "started"


def _timesfm_json(s: Session) -> dict:
    st = tsm.get_state(s)
    reg = s.scalar(select(ModelRegistryRow).where(ModelRegistryRow.name == "timesfm"))
    return {"enabled": st.enabled, "mode": st.mode, "status": tsm.get_status(s),
            "changes": tsm.changes(s)[-20:][::-1], "model": tsm.VERSION, "repo": tsm.REPO,
            "license_note": tsm.LICENSE_NOTE, "leakage_note": tsm.LEAKAGE_NOTE,
            "registry": {"status": reg.status, "validation": reg.validation} if reg else None,
            "modes": {"feature": "TimesFM's forecast is one input and can raise or lower confidence.",
                      "filter": "Keep a signal only if TimesFM's 3-10 day forecast is positive."}}


def router(db: Callable, auth: Callable) -> APIRouter:
    r = APIRouter()

    @r.get("/api/timesfm", dependencies=[Depends(auth)])
    def get_timesfm(s: Session = Depends(db)) -> dict:
        return _timesfm_json(s)

    @r.put("/api/timesfm", dependencies=[Depends(auth)])
    def put_timesfm(body: TimesFMBody, s: Session = Depends(db)) -> dict:
        tsm.set_state(s, body.enabled, body.mode, by="dashboard")
        s.commit()
        out = _timesfm_json(s)
        # Either way the change applies at once: forecasts (if on) and a re-score run in the background.
        out["refresh"] = _queue_timesfm_refresh() if body.enabled else _rescore_in_background()
        return out

    @r.get("/api/timesfm/filtered", dependencies=[Depends(auth)])
    def filtered(s: Session = Depends(db)) -> dict:
        latest = s.scalar(select(Signal.as_of_date).order_by(Signal.as_of_date.desc()).limit(1))
        if latest is None:
            return {"as_of": None, "signals": []}
        rows = s.scalars(select(Signal).where(Signal.as_of_date == latest)).all()
        out = []
        for sig in rows:
            f = sig.features or {}
            if f.get("skip_reason") != "filtered_by_timesfm":
                continue
            t = s.get(Ticker, sig.symbol)
            tf = f.get("timesfm") or {}
            out.append({"id": sig.id, "symbol": sig.symbol, "company": t.name if t else sig.symbol,
                        "catalyst": sig.catalyst_type, "confidence": round(sig.confidence, 1),
                        "reason": tf.get("note"), "forecast": tf.get("forecast")})
        return {"as_of": latest.isoformat(), "signals": sorted(out, key=lambda x: -x["confidence"])}

    @r.get("/api/catalysts", dependencies=[Depends(auth)])
    def catalysts(s: Session = Depends(db)) -> dict:
        from catalystedge.signals.catalyst_status import catalyst_status

        return {**catalyst_report(s), "status": catalyst_status(s)}

    @r.get("/api/evidence", dependencies=[Depends(auth)])
    def evidence_(s: Session = Depends(db)) -> dict:
        return evidence(s)

    @r.get("/api/backtest/latest", dependencies=[Depends(auth)])
    def backtest_latest(s: Session = Depends(db)) -> dict:
        bt = latest_backtest(s)
        if bt is None:
            return {"run": None, "how_to_run": "cd backend && uv run catalystedge backtest --timesfm"}
        return {"run": {"id": bt.id, "finished_at": bt.finished_at.isoformat() if bt.finished_at else None,
                        "sources": bt.data_sources, "families": bt.event_families}, "report": bt.report}

    return r
