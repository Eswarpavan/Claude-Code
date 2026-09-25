"""Which catalyst rules are switched on (user rule: "only enable a rule if it beats the baselines").

The strict bar, the same as the backtest's: ON only with >= 50 trades that beat the S&P 500 over the
same days (win rate, average return and Sharpe) with p < 0.05 after a Bonferroni correction.
Tested on >= 30 trades and failed -> OFF. Fewer than 30 -> 'untested' (shown, labelled unproven).

Evidence, newest wins per catalyst:
  backtest  the latest walk-forward run that carries the strict slice tests; otherwise the verdicts
            shipped with the app (signals/baseline_verdicts.json)
  live      once a catalyst has >= 50 signals with a 10-day outcome, the same test on live outcomes
            (Bonferroni over the catalysts) decides
Disabled catalysts are still scored and logged (so their outcomes keep being measured) but never displayed.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.signals.priors import CATALYSTS
from catalystedge.signals.report import latest_backtest

MIN_LIVE = 50
MIN_JUDGE = 30
LIVE_HORIZON = 10
BASELINE = Path(__file__).with_name("baseline_verdicts.json")


def baseline_verdicts() -> dict[str, dict]:
    """Verdicts of the real backtest, shipped with the app so a fresh database (a new computer, Neon)
    starts from the same evidence instead of treating every catalyst as untested."""
    data = json.loads(BASELINE.read_text())
    return {c: {**v, "why": f"{v['why']} [{data['source'].split(';')[0]}]"} for c, v in data["verdicts"].items()}


def _backtest_verdicts(session: Session) -> tuple[dict, str]:
    bt = latest_backtest(session)
    report = (bt.report or {}) if bt else {}
    if (report.get("slices") or {}).get("slices"):
        from catalystedge.backtest.slices import strict_catalyst_verdicts

        return strict_catalyst_verdicts(report["slices"], report.get("catalyst_verdicts")), "backtest"
    return baseline_verdicts(), "baseline backtest"      # no backtest here, or one from before the strict bar


def _live_tests(session: Session) -> dict[str, dict]:
    """Per catalyst: the strict test on live 10-day outcomes of every logged signal scoring >= 65."""
    from catalystedge.backtest.slices import ALPHA, sign_flip_p
    from catalystedge.backtest.walkforward import beats, stats
    from catalystedge.db.models import Signal, SignalOutcome

    rows = session.execute(select(Signal.catalyst_type, SignalOutcome.return_pct, SignalOutcome.excess_vs_spy_pct)
                           .join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                           .where(SignalOutcome.horizon_days == LIVE_HORIZON, Signal.rule_score >= 65.0)).all()
    by: dict[str, list[tuple[float, float]]] = {}
    for cat, ret, exc in rows:
        if exc is not None:
            by.setdefault(cat, []).append((ret, ret - exc))
    m = len(CATALYSTS)
    out = {}
    for cat, pairs in by.items():
        st = stats([r for r, _ in pairs], [LIVE_HORIZON] * len(pairs))
        spy = stats([s for _, s in pairs], [LIVE_HORIZON] * len(pairs))
        p = sign_flip_p([r - s for r, s in pairs])
        p_corr = min(1.0, p * m) if p is not None else None
        passed = (len(pairs) >= MIN_LIVE and beats(st, spy) and (st["mean_pct"] or 0) > 0
                  and p_corr is not None and p_corr < ALPHA)
        out[cat] = {"n": len(pairs), "passed": passed, "p": p, "p_corr": p_corr, "stats": st, "spy": spy}
    return out


def catalyst_status(session: Session) -> dict[str, dict]:
    verdicts, basis_name = _backtest_verdicts(session)
    live = _live_tests(session)
    out: dict[str, dict] = {}
    for c in CATALYSTS:
        v = verdicts.get(c)
        status = v["status"] if v else "untested"
        why = v["why"] if v else "no backtest for this catalyst yet"
        basis = basis_name if v else "none"
        lv = live.get(c)
        if lv and lv["n"] >= MIN_LIVE:
            status = "enabled" if lv["passed"] else "disabled"
            why = (f"{lv['n']} live signals: {lv['stats']['hit_rate'] * 100:.0f}% winners, "
                   f"{lv['stats']['mean_pct'] - lv['spy']['mean_pct']:+.2f}% vs the S&P 500 per signal, "
                   f"p = {lv['p']:.3f} ({lv['p_corr']:.3f} after correction); "
                   + ("passes the strict bar" if lv["passed"] else "fails the strict bar"))
            basis = "live"
        out[c] = {"status": status, "why": why, "basis": basis}
    return out
