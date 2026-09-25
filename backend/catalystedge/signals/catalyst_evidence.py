"""What the backtest says each catalyst type has actually done, for display next to a signal.

No promises: these are historical out-of-sample results of the rules strategy for that catalyst
(after costs), with the S&P 500 over the same days for comparison. A catalyst with no or few
historical trades says so instead of showing a number that looks meaningful but isn't.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from catalystedge.signals.catalyst_status import BASELINE
from catalystedge.signals.priors import CATALYSTS, PRIORS
from catalystedge.signals.report import latest_backtest

MIN_RELIABLE = 30      # same threshold the backtest uses before it judges a catalyst


def _label(c: str) -> str:
    return PRIORS[c].plain if c in PRIORS else c.replace("_", " ")


def _source() -> tuple[dict, str | None]:
    """(catalyst -> verdict dict with 'rules' / 'spy_same_days' stats, source description)."""
    data = json.loads(BASELINE.read_text())
    return data.get("verdicts", {}), data.get("source")


def confidence_check(session: Session) -> dict | None:
    """Did higher confidence mean better trades in the backtest? (the confidence diagnostic)"""
    bt = latest_backtest(session)
    d = ((bt.report or {}).get("diagnostics") or {}) if bt else {}
    if not d:
        d = json.loads(BASELINE.read_text()).get("confidence_check") or {}
    if not d:
        return None
    worse = d.get("worse") or []
    return {"reliable": not worse, "worse": worse,
            "text": ("In the backtest, higher-confidence signals did worse than lower-confidence ones ("
                     + "; ".join(worse) + "). The confidence number does not rank signals: do not treat a higher "
                     "number as a better trade. Look at the catalyst's history below instead.") if worse else
            "In the backtest, higher-confidence signals did not do worse than lower-confidence ones; confidence "
            "is still UNCALIBRATED, so it is not a probability."}


def catalyst_evidence(session: Session) -> dict[str, dict]:
    bt = latest_backtest(session)
    verdicts = ((bt.report or {}).get("catalyst_verdicts") or {}) if bt else {}
    source = f"backtest of {bt.finished_at:%Y-%m-%d}" if verdicts and bt and bt.finished_at else None
    if not verdicts:
        verdicts, src = _source()
        source = f"shipped {src.split(';')[0]}" if src else None
    out = {}
    for c in CATALYSTS:
        v = verdicts.get(c) or {}
        st, spy = v.get("rules") or {}, v.get("spy_same_days") or {}
        n = st.get("n") or 0
        e = {"catalyst": c, "label": _label(c), "source": source, "status": v.get("status", "untested"),
             "n": n, "win_rate": st.get("hit_rate"), "avg_return_pct": st.get("mean_pct"), "sharpe": st.get("sharpe"),
             "spy_win_rate": spy.get("hit_rate"), "spy_avg_return_pct": spy.get("mean_pct"),
             "enough_data": n >= MIN_RELIABLE}
        if not n and e["status"] in ("enabled", "disabled"):
            e["text"] = (f"{_label(c).capitalize()}: switched {'on' if e['status'] == 'enabled' else 'off'} by the "
                         f"backtest ({v.get('why')}); its per-trade numbers are not on this install yet.")
        elif not n:
            why = v.get("why") or "no historical trades for this catalyst"
            e["text"] = f"{_label(c).capitalize()}: not enough data yet ({why})."
        else:
            base = (f"{_label(c).capitalize()}: avg {st['mean_pct']:+.2f}% per trade, win rate "
                    f"{st['hit_rate'] * 100:.0f}%, {n} trades")
            if spy.get("mean_pct") is not None:
                base += f" (S&P 500 same days: avg {spy['mean_pct']:+.2f}%, win rate {spy['hit_rate'] * 100:.0f}%)"
            e["text"] = base + ("." if n >= MIN_RELIABLE else f". Too few trades to rely on (needs {MIN_RELIABLE}).")
        out[c] = e
    return out
