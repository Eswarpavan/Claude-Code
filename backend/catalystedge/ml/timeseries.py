"""TimesFM (Google Research time-series foundation model): optional, OFF by default.

Model: TimesFM 3.0 (`google/timesfm-3.0-pytorch`, pip `timesfm[torch]` 3.0.x), the newest
release as of 2026-09 (checked on PyPI and Hugging Face). Runs on CPU (~1.3 GB weights).
LICENSE: the 3.0 weights are under Google's `timesfm-non-commercial-license-v1.0`
(non-commercial, non-production). A personal paper-trading app is non-commercial; if you
ever use this commercially, switch to TimesFM 2.5 (Apache-2.0) or turn it off.

LEAKAGE: TimesFM was pretrained on a large public corpus that very likely includes daily
stock prices up to its 2026 release. Any backtest period before that may overlap its
training data and make it look better than it really is. The report says so every time.

State (app_settings):
  timesfm            {"enabled": bool, "mode": "feature" | "filter"}      (default off)
  timesfm_changes    last 100 changes [{at, enabled, mode, by}]
  timesfm_status     {"status": off|ready|running|model missing|failed, "detail", "updated_at"}
  timesfm_forecasts  {"as_of": date, "forecasts": {symbol: Forecast dict}}
The page never waits for TimesFM: forecasts come from a background job and are read from
this cache; anything missing or failed falls back to the normal pipeline with a warning.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.db.models import AppSetting, Event, ModelRegistryRow
from catalystedge.signals.timesfm_hook import MODES, Forecast, TimesFMState

log = logging.getLogger("catalystedge.timesfm")

REPO = "google/timesfm-3.0-pytorch"
VERSION = "timesfm-3.0"
HORIZON = 10
CONTEXT = 512
LEAKAGE_NOTE = ("TimesFM 3.0 was pretrained on public time series that very likely include daily stock prices "
                "up to 2026. This backtest period overlaps that window, so TimesFM's results here may be "
                "optimistic (leakage). Trust live outcomes after its release date more.")
LICENSE_NOTE = "TimesFM 3.0 weights: non-commercial, non-production license (fine for personal paper trading)."


# ----------------------------------------------------------------------------- settings


def _get(session: Session, key: str, default: Any) -> Any:
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def _put(session: Session, key: str, value: Any) -> None:
    stmt = insert(AppSetting).values(key=key, value=value, updated_at=dt.datetime.now(dt.UTC))
    session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value,
                                                                            "updated_at": stmt.excluded.updated_at}))
    session.flush()
    session.expire_all()


def get_state(session: Session) -> TimesFMState:
    v = _get(session, "timesfm", {}) or {}
    mode = v.get("mode") if v.get("mode") in MODES else "feature"
    return TimesFMState(bool(v.get("enabled", False)), mode)


def set_state(session: Session, enabled: bool, mode: str | None = None, by: str = "user") -> TimesFMState:
    """Persist the toggle (takes effect on the next read; no restart) and log the change."""
    old = get_state(session)
    mode = mode if mode in MODES else old.mode
    new = TimesFMState(bool(enabled), mode)
    _put(session, "timesfm", {"enabled": new.enabled, "mode": new.mode})
    changes = list(_get(session, "timesfm_changes", []) or [])
    changes.append({"at": dt.datetime.now(dt.UTC).isoformat(), "enabled": new.enabled, "mode": new.mode, "by": by,
                    "was": {"enabled": old.enabled, "mode": old.mode}})
    _put(session, "timesfm_changes", changes[-100:])
    log.info("timesfm toggled", extra={"enabled": new.enabled, "mode": new.mode, "by": by})
    if not new.enabled:
        set_status(session, "off", "switched off")
    return new


def changes(session: Session) -> list[dict]:
    return list(_get(session, "timesfm_changes", []) or [])


def get_status(session: Session) -> dict:
    return _get(session, "timesfm_status", {"status": "off", "detail": "never run"}) or {}


def set_status(session: Session, status: str, detail: str = "") -> None:
    _put(session, "timesfm_status", {"status": status, "detail": detail,
                                     "updated_at": dt.datetime.now(dt.UTC).isoformat()})


def cached_forecasts(session: Session, as_of: dt.date) -> dict[str, Forecast]:
    v = _get(session, "timesfm_forecasts", {}) or {}
    if v.get("as_of") != as_of.isoformat():
        return {}
    return {s: Forecast(**f) for s, f in (v.get("forecasts") or {}).items()}


# ----------------------------------------------------------------------------- model


def ml_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("timesfm3") is not None and importlib.util.find_spec("torch") is not None


def local_path(models_dir: Path) -> Path:
    return Path(models_dir) / VERSION


class TimesFMService:
    def __init__(self, models_dir: Path, auto_download: bool = True):
        self.path = local_path(models_dir)
        self.auto_download = auto_download
        self._model = None

    def status(self) -> tuple[str, str]:
        if not ml_available():
            return "model missing", "TimesFM not installed (uv sync --extra ml --extra timeseries)"
        if not (self.path / "model.safetensors").exists():
            return "model missing", f"weights not downloaded yet ({REPO}, ~1.3 GB)"
        return "ready", f"{VERSION} at {self.path}"

    def ensure(self) -> None:
        if not ml_available():
            raise RuntimeError("TimesFM not installed (uv sync --extra ml --extra timeseries)")
        if not (self.path / "model.safetensors").exists():
            if not self.auto_download:
                raise RuntimeError("TimesFM weights missing and auto-download is off")
            from huggingface_hub import snapshot_download

            snapshot_download(REPO, local_dir=str(self.path))

    def model(self):
        if self._model is None:
            self.ensure()
            from timesfm3.torch.timesfm3_forecaster import TimesFM3Forecaster

            self._model = TimesFM3Forecaster.from_pretrained(str(self.path), device="cpu")
        return self._model

    def forecast(self, closes: dict[str, list[float]], as_of: dt.date) -> dict[str, Forecast]:
        import numpy as np

        syms = [s for s, c in closes.items() if len(c) >= 32]
        if not syms:
            return {}
        ctx = [np.asarray(closes[s][-CONTEXT:], dtype=np.float32) for s in syms]
        # Prices cannot go below zero: ask TimesFM for non-negative forecasts.
        outs = list(self.model().predict_batch(ctx, horizon=HORIZON, return_quantiles=True, make_positive=True))
        return {s: summarize(s, closes[s][-1], o.forecast, o.quantiles, as_of) for s, o in zip(syms, outs, strict=True)}

    def health_check(self) -> tuple[bool, str]:
        import math

        try:
            f = self.forecast({"TEST": [100 + math.sin(i / 5) for i in range(128)]}, dt.date.today())
            ok = "TEST" in f and all(map(math.isfinite, (f["TEST"].expected_return_pct, f["TEST"].low_pct)))
            return ok, "forecast ok" if ok else "non-finite forecast"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


def summarize(symbol: str, last_close: float, median, quantiles, as_of: dt.date) -> Forecast:
    """3-10 trading-day view: average of days 3..10 of the median path and of the 10%/90% quantiles."""
    days = slice(2, HORIZON)
    er = (float(median[days].mean()) / last_close - 1) * 100
    lo = (float(quantiles[days, 0].mean()) / last_close - 1) * 100
    hi = (float(quantiles[days, -1].mean()) / last_close - 1) * 100
    lo, er = max(lo, -100.0), max(er, -100.0)
    return Forecast(symbol, HORIZON, er, min(lo, er), max(hi, er), VERSION, as_of.isoformat())


def _upsert_registry(session: Session, status: str, validation: dict | None = None) -> None:
    row = session.scalar(select(ModelRegistryRow).where(ModelRegistryRow.name == "timesfm"))
    if row is None:
        row = ModelRegistryRow(name="timesfm", kind="ts_foundation", version=VERSION, source_uri=f"hf:{REPO}")
        session.add(row)
    row.status = {"ready": "ready", "failed": "failed"}.get(status, "missing")
    if validation is not None:
        row.validation = validation
        row.validated_at = dt.datetime.now(dt.UTC)
    session.flush()


# ----------------------------------------------------------------------------- jobs


def run_timesfm_job(session: Session, models_dir: Path, now: dt.datetime, service: TimesFMService | None = None
                    ) -> dict:
    """Background job: forecast the tickers that already have a positive event in the window.
    Never raises; the status says what happened."""
    from catalystedge.core import calendar
    from catalystedge.pipeline.window import cutoff
    from catalystedge.prices import load_bars
    from catalystedge.signals.priors import PRIORS

    state = get_state(session)
    if not state.enabled:
        set_status(session, "off", "switched off")
        return {"status": "off"}
    svc = service or TimesFMService(models_dir)
    st, detail = svc.status()
    if st != "ready" and not svc.auto_download:
        set_status(session, st, detail)
        _upsert_registry(session, st)
        return {"status": st, "detail": detail}
    set_status(session, "running", "forecasting")
    as_of = calendar.last_completed_session(now)
    syms = sorted(set(session.scalars(select(Event.symbol).where(
        Event.polarity == "positive", Event.event_type.in_(tuple(PRIORS)), Event.available_at > cutoff(now),
        Event.available_at <= now))))
    closes = {s: [float(b.close) for b in load_bars(session, s, now, lookback_sessions=CONTEXT)] for s in syms}
    try:
        fc = svc.forecast(closes, as_of)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:300]
        status = "model missing" if "not installed" in msg or "missing" in msg else "failed"
        set_status(session, status, msg)
        _upsert_registry(session, status)
        return {"status": status, "detail": msg}
    _put(session, "timesfm_forecasts", {"as_of": as_of.isoformat(),
                                        "forecasts": {s: f.__dict__ for s, f in fc.items()}})
    set_status(session, "ready", f"{len(fc)} forecasts for {as_of.isoformat()}")
    _upsert_registry(session, "ready")
    return {"status": "ready", "forecasts": len(fc), "as_of": as_of.isoformat()}


def engine_kwargs(session: Session, now: dt.datetime, models_dir: Path) -> dict:
    """Everything optional the signal engine should use right now: the validated ranker (only if
    enabled), the calibrator (only with a passing calibration), and TimesFM (only if switched on)."""
    from catalystedge.core import calendar
    from catalystedge.ml.ranker import load_enabled_ranker
    from catalystedge.signals.calibration import SnapshotCalibrator

    state = get_state(session)
    kwargs: dict = {"timesfm_state": state}
    ranker = load_enabled_ranker(session, models_dir)
    if ranker is not None:
        kwargs["ranker"] = ranker
    cal = SnapshotCalibrator(session)
    if cal.active:
        kwargs["calibrator"] = cal
    if state.enabled:
        st = get_status(session).get("status", "off")
        kwargs["timesfm_forecasts"] = cached_forecasts(session, calendar.last_completed_session(now))
        kwargs["timesfm_status"] = "ready" if st == "ready" else st
    return kwargs


def backtest_forecaster(settings) -> tuple[Any, str]:
    """For `catalystedge backtest --timesfm`: forecasts at each row's decision close from bars up
    to that close only (no lookahead on our side; the pretraining overlap is reported)."""
    svc = TimesFMService(Path(settings.models_dir))

    def forecaster(rows, bars) -> dict[int, tuple[float, float, float]]:
        batch: list[tuple[int, str, list[float], dt.date]] = []
        for i, r in enumerate(rows):
            b = bars.get(r.event.symbol) or []
            closes = [x.close for x in b if x.date <= r.decision_date][-CONTEXT:]
            if len(closes) >= 32:
                batch.append((i, f"{i}", closes, r.decision_date))
        out: dict[int, tuple[float, float, float]] = {}
        for k in range(0, len(batch), 64):
            chunk = batch[k:k + 64]
            fc = svc.forecast({key: c for _, key, c, _ in chunk}, chunk[0][3])
            for i, key, _, _ in chunk:
                if key in fc:
                    f = fc[key]
                    out[i] = (f.expected_return_pct, f.low_pct, f.high_pct)
        return out

    return forecaster, LEAKAGE_NOTE
