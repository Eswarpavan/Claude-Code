"""Which catalyst rules are switched on (user rule: "only enable a rule if it beats the baselines").

Two kinds of evidence, newest wins per catalyst:
  backtest  the latest walk-forward run's catalyst_verdicts (enabled / disabled / untested)
  live      once a catalyst has >= 30 displayed signals with a 10-day outcome, it is disabled if it
            did not beat SPY (hit rate <= 50% or average excess return vs SPY <= 0)
'untested' catalysts stay on but every signal says "unproven". Disabled catalysts are still
scored and logged (so their outcomes keep being measured) but never displayed.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from catalystedge.signals.priors import CATALYSTS
from catalystedge.signals.report import latest_backtest, live_by_catalyst

MIN_LIVE = 30
BASELINE = Path(__file__).with_name("baseline_verdicts.json")


def baseline_verdicts() -> dict[str, dict]:
    """Verdicts of the real backtest, shipped with the app so a fresh database (a new computer, Neon)
    starts from the same evidence instead of treating every catalyst as untested."""
    data = json.loads(BASELINE.read_text())
    return {c: {**v, "why": f"{v['why']} [{data['source'].split(';')[0]}]"} for c, v in data["verdicts"].items()}


def catalyst_status(session: Session) -> dict[str, dict]:
    bt = latest_backtest(session)
    verdicts = ((bt.report or {}).get("catalyst_verdicts") or {}) if bt else {}
    basis_name = "backtest"
    if not verdicts:
        verdicts, basis_name = baseline_verdicts(), "baseline backtest"
    # Every logged signal that scored >= 65, shown or not, so a switched-off catalyst keeps being
    # measured and can switch back on.
    live = live_by_catalyst(session, displayed_only=False, min_rule_score=65.0)
    out: dict[str, dict] = {}
    for c in CATALYSTS:
        v = verdicts.get(c)
        status = v["status"] if v else "untested"
        why = v["why"] if v else "no backtest for this catalyst yet"
        basis = basis_name if v else "none"
        ten = (live.get(c) or {}).get("10d")
        if ten and ten["n"] >= MIN_LIVE:
            beat = (ten["hit_rate"] or 0) > 0.5 and (ten.get("mean_excess_vs_spy_pct") or 0) > 0
            status = "enabled" if beat else "disabled"
            why = (f"{ten['n']} live signals: {ten['hit_rate'] * 100:.0f}% winners, "
                   f"{(ten.get('mean_excess_vs_spy_pct') or 0):+.2f}% vs S&P 500 on average")
            basis = "live"
        out[c] = {"status": status, "why": why, "basis": basis}
    return out
