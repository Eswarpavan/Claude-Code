"""`catalystedge backtest`: build rows, walk forward, persist the evidence.

Writes:
  backtest_runs / backtest_trades   the run, its report JSON and the rules strategy's trades
  calibration_snapshots             out-of-fold reliability of the rule confidence
  model_registry                    ranker-lgbm (enabled only if it beat every baseline)
  models/ranker/                    the final LightGBM model + metadata (always saved, used only if enabled)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.backtest.dataset import Bar, Row, build_rows
from catalystedge.backtest.events import load_events
from catalystedge.backtest.walkforward import (
    blended,
    build_report,
    calibration_snapshots,
    fit_final_model,
    run_walk_forward,
)
from catalystedge.db.models import BacktestRun, BacktestTrade, CalibrationSnapshot, ModelRegistryRow, PriceDaily
from catalystedge.ml.ranker import RANKER_NAME
from catalystedge.ml.sentiment import SentimentModel
from catalystedge.pipeline.ticker_link import Universe
from catalystedge.signals.engine import DISPLAY_MIN


def load_bars(session: Session, symbols: list[str]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for sym in symbols:
        rows = session.scalars(select(PriceDaily).where(PriceDaily.symbol == sym).order_by(PriceDaily.date)).all()
        if rows:
            out[sym] = [Bar(r.date, float(r.open), float(r.high), float(r.low), float(r.close), int(r.volume))
                        for r in rows]
    return out


def _upsert_model(session: Session, name: str, kind: str, version: str, uri: str, path: str | None, status: str,
                  enabled: bool, validation: dict, beats: bool | None) -> None:
    row = session.scalar(select(ModelRegistryRow).where(ModelRegistryRow.name == name))
    if row is None:
        row = ModelRegistryRow(name=name, kind=kind, version=version, source_uri=uri)
        session.add(row)
    row.version, row.source_uri, row.local_path, row.status = version, uri, path, status
    row.enabled, row.validation, row.beats_baseline = enabled, validation, beats
    row.validated_at = dt.datetime.now(dt.UTC)
    session.flush()


def run_backtest(session: Session, *, cache_dir: Path, symbols: list[str], universe: Universe,
                 model: SentimentModel | None, models_dir: Path,
                 timesfm_forecaster: Callable[[list[Row], dict[str, list[Bar]]], dict[int, tuple]] | None = None,
                 timesfm_leakage_note: str | None = None, log: Callable[[str], None] = print,
                 raw_out: Path | None = None, raw_md: Path | None = None) -> dict:
    started = dt.datetime.now(dt.UTC)
    events = load_events(cache_dir, symbols, universe, model)
    log(f"{len(events)} historical positive events")
    bars = load_bars(session, symbols + ["SPY"])
    rows = build_rows(events, bars, bars.get("SPY", []))
    log(f"{len(rows)} backtest rows ({sum(r.trade_return is not None for r in rows)} with a completed trade)")
    wf = run_walk_forward(rows)
    fc = None
    if timesfm_forecaster is not None:
        try:
            fc = timesfm_forecaster(wf.rows, bars)
            log(f"TimesFM forecasts for {len(fc)} rows")
        except Exception as e:  # TimesFM must never break the backtest
            log(f"TimesFM unavailable: {type(e).__name__}: {e}")
            fc = None
    note = f"{len(bars) - 1} of {len(symbols)} symbols with prices; SEC 8-K/Form 4 + Finnhub earnings + openFDA"
    report = build_report(wf, note, fc, timesfm_leakage_note)
    report["n_events"] = len(events)
    report["generated_at"] = dt.datetime.now(dt.UTC).isoformat()
    report["note"] = note

    run = BacktestRun(started_at=started, finished_at=dt.datetime.now(dt.UTC),
                      params={"symbols": len(symbols), "display_min": DISPLAY_MIN, "embargo_days": 14,
                              "label": "paper-exit trade net > 0"},
                      data_sources=["sec_edgar", "finnhub_earnings", "openfda", "tiingo_eod"],
                      event_families=sorted({r.catalyst for r in rows}), status="done", report=report)
    session.add(run)
    session.flush()
    for i in sorted(wf.probs):
        r = wf.rows[i]
        if r.rule_score >= DISPLAY_MIN and not r.skip_reason and r.trade_exit_date:
            session.add(BacktestTrade(
                run_id=run.id, symbol=r.event.symbol, event_ref=f"{r.catalyst}|{r.event.ref}|{r.event.headline[:200]}",
                decision_date=r.decision_date, entry_date=r.entry_date,
                entry_price=Decimal(str(round(r.entry_price, 4))),
                exit_date=r.trade_exit_date, exit_price=Decimal(str(round(
                    r.entry_price * (1 + (r.trade_return + r.cost_pct) / 100), 4))),
                exit_reason=r.trade_exit_reason or "time_stop", return_pct=round(r.trade_return, 4),
                confidence=round(blended(r, wf.probs[i], wf.weights[i]), 2)))
    for snap in calibration_snapshots(wf):
        session.add(CalibrationSnapshot(basis="backtest", event_family=snap["event_family"], model_version="rules-v1",
                                        n=snap["n"], buckets=snap["buckets"] + [{"mapping": snap["mapping"]}],
                                        brier=snap["brier"], ece=snap["ece"], verdict=snap["verdict"]))
    enabled = bool(report["verdict"]["model_enabled"])
    weights = [f["weight"] for f in report["folds"] if f["weight"] is not None]
    path = models_dir / "ranker"
    if any(r.trade_return is not None for r in rows):
        meta = fit_final_model(rows, path, weights[-1] if weights else 0.5)
        _upsert_model(session, RANKER_NAME, "ranker", f"lgbm-{meta['trained_at'][:10]}", "local:backtest",
                      str(path.resolve()), "ready", enabled,
                      {"run_id": run.id, "strategies": report["strategies"], "verdict": report["verdict"]}, enabled)
    session.flush()
    report["run_id"] = run.id
    if raw_out is not None:
        from catalystedge.backtest.raw import export

        files = export(wf, fc, report, raw_out, raw_md or raw_out.parent / "BACKTEST_RAW.md")
        log("raw results: " + ", ".join(str(p) for p in files.values()))
    return report
