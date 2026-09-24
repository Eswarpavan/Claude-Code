"""Which catalysts actually work: hit rate and average return by catalyst type.

Two sources, shown side by side so the evidence is never mixed up:
  live      every signal ever displayed, with its 1/3/10 trading-day outcome (net of costs)
  backtest  the latest walk-forward run (out-of-sample, rules strategy and all events)
"""

from __future__ import annotations

import statistics

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catalystedge.db.models import BacktestRun, PaperPosition, Signal, SignalOutcome


def live_by_catalyst(session: Session, displayed_only: bool = True, min_rule_score: float | None = None) -> dict:
    q = select(Signal.catalyst_type, SignalOutcome.horizon_days, SignalOutcome.return_pct,
               SignalOutcome.excess_vs_spy_pct).join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
    if displayed_only:
        q = q.where(Signal.displayed.is_(True))
    if min_rule_score is not None:
        q = q.where(Signal.rule_score >= min_rule_score)
    acc: dict[str, dict[int, list[tuple[float, float | None]]]] = {}
    for cat, h, ret, exc in session.execute(q):
        acc.setdefault(cat, {}).setdefault(h, []).append((ret, exc))
    out = {}
    for cat, by_h in acc.items():
        out[cat] = {}
        for h, vals in sorted(by_h.items()):
            rets = [v[0] for v in vals]
            excess = [v[1] for v in vals if v[1] is not None]
            out[cat][f"{h}d"] = {"n": len(rets), "hit_rate": round(sum(r > 0 for r in rets) / len(rets), 4),
                                 "mean_pct": round(statistics.mean(rets), 4),
                                 "mean_excess_vs_spy_pct": round(statistics.mean(excess), 4) if excess else None}
    return out


def latest_backtest(session: Session) -> BacktestRun | None:
    return session.scalar(select(BacktestRun).where(BacktestRun.status == "done")
                          .order_by(BacktestRun.finished_at.desc()).limit(1))


def evidence(session: Session, min_closed_trades: int = 30) -> dict:
    """What the dashboard shows next to every confidence number (rule 6)."""
    closed = session.scalar(select(func.count()).select_from(PaperPosition)
                            .where(PaperPosition.status == "closed")) or 0
    bt = latest_backtest(session)
    shown = session.scalar(select(func.count()).select_from(Signal).where(Signal.displayed.is_(True))) or 0
    with_outcome = session.scalar(select(func.count(func.distinct(SignalOutcome.signal_id)))) or 0
    calibrated_possible = closed >= min_closed_trades or bt is not None
    return {
        "closed_paper_trades": closed, "min_closed_trades": min_closed_trades,
        "signals_shown": shown, "signals_with_outcomes": with_outcome,
        "backtest_run_id": bt.id if bt else None,
        "backtest_period": (bt.report or {}).get("period") if bt else None,
        "backtest_verdict": (bt.report or {}).get("verdict") if bt else None,
        "label": "UNCALIBRATED" if not calibrated_possible else "see calibration verdict",
        "plain": (f"Confidence is UNCALIBRATED: {closed} of {min_closed_trades} closed paper trades and "
                  f"{'no' if bt is None else 'a'} historical backtest." if not calibrated_possible else
                  "Evidence exists; each signal shows whether its family's calibration passed."),
    }


def catalyst_report(session: Session) -> dict:
    bt = latest_backtest(session)
    rep = (bt.report or {}) if bt else {}
    return {"live": live_by_catalyst(session),
            "backtest": {"run_id": bt.id if bt else None, "period": rep.get("period"),
                         "rules": rep.get("by_catalyst", {}).get("rules", {}),
                         "all_events": rep.get("by_catalyst", {}).get("all_events", {})},
            "evidence": evidence(session)}
