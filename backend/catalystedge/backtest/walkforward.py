"""Walk-forward evaluation, LightGBM ranker, calibration and the evidence report.

Folds: expanding window, quarterly test periods. Training rows must have decided at
least EMBARGO_DAYS before the test period AND have exited before it (purged), so no
test-period price ever reaches training (rule 7). All returns are net of costs.

Strategies compared on the same out-of-sample rows:
  all_events   buy every positive event (naive baseline)
  rules        the live rule: score >= 65, not priced in
  model        blended confidence >= 65, not priced in (LightGBM + rules)
  spy          SPY over the identical holding windows (buying the S&P 500 instead)
  (+ TimesFM variants when forecasts are supplied)
The ranker is enabled only if `model` beats all_events, rules AND spy on hit rate,
mean return and Sharpe; otherwise it stays off and the report says so.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from catalystedge.backtest.dataset import Row
from catalystedge.signals.engine import DISPLAY_MIN, HIGHLIGHT_MIN
from catalystedge.signals.priors import CATALYSTS
from catalystedge.signals.vector import FEATURES, vectorize

EMBARGO_DAYS = 14
MIN_TRAIN = 150
BUCKETS = ((0, 50), (50, DISPLAY_MIN), (DISPLAY_MIN, HIGHLIGHT_MIN), (HIGHLIGHT_MIN, 101))
LGB_PARAMS = dict(objective="binary", learning_rate=0.05, n_estimators=200, num_leaves=15, min_child_samples=20,
                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1,
                  random_state=7)
WEIGHTS = (0.25, 0.5, 0.75, 1.0)
# The ranker is enabled only with enough out-of-sample trades AND a selection that random picks
# from the same events rarely match (one-sided permutation test).
MIN_MODEL_TRADES = 50
SIGNIFICANCE = 0.05


# ----------------------------------------------------------------------------- metrics


def stats(returns: Sequence[float], holds: Sequence[int] | None = None) -> dict:
    r = [x for x in returns if x is not None and not math.isnan(x)]
    if not r:
        return {"n": 0, "hit_rate": None, "mean_pct": None, "median_pct": None, "sharpe": None}
    sd = statistics.pstdev(r) if len(r) > 1 else 0.0
    hold = statistics.mean(holds) if holds else 5.0
    sharpe = (statistics.mean(r) / sd * math.sqrt(252 / max(hold, 1))) if sd > 0 and len(r) >= 5 else None
    return {"n": len(r), "hit_rate": round(sum(x > 0 for x in r) / len(r), 4), "mean_pct": round(statistics.mean(r), 4),
            "median_pct": round(statistics.median(r), 4), "sharpe": round(sharpe, 3) if sharpe is not None else None}


def _trade_stats(rows: Sequence[Row]) -> dict:
    return stats([r.trade_return for r in rows], [r.trade_sessions or 5 for r in rows])


def selection_p_value(picked: Sequence[float], pool: Sequence[float], n_perm: int = 5000,
                      seed: int = 11) -> float | None:
    """One-sided permutation test: how often does a RANDOM subset of the same size from the same
    pool of trades average at least as much as the picked subset? Small = the selection has skill."""
    import random

    picked = [x for x in picked if x is not None]
    pool = [x for x in pool if x is not None]
    if len(picked) < 5 or len(pool) <= len(picked):
        return None
    target = statistics.mean(picked)
    rnd = random.Random(seed)
    hits = sum(statistics.mean(rnd.sample(pool, len(picked))) >= target for _ in range(n_perm))
    return round((hits + 1) / (n_perm + 1), 4)


def beats(a: dict, b: dict) -> bool:
    keys = ("hit_rate", "mean_pct", "sharpe")
    return all(a.get(k) is not None and b.get(k) is not None and a[k] > b[k] for k in keys)


def ece(probs: Sequence[float], hits: Sequence[bool], bins: int = 10) -> tuple[float, list[dict]]:
    out, total, err = [], len(probs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        p_mean = sum(probs[i] for i in idx) / len(idx)
        h_mean = sum(hits[i] for i in idx) / len(idx)
        err += len(idx) / total * abs(p_mean - h_mean)
        out.append({"lo": lo, "hi": hi, "n": len(idx), "predicted": round(p_mean, 4), "observed": round(h_mean, 4)})
    return round(err, 4), out


def verdict(ece_value: float, buckets: list[dict], min_n: int = 50) -> str:
    if not buckets or any(b["n"] < min_n for b in buckets):
        return "insufficient"
    observed = [b["observed"] for b in buckets]
    monotonic = all(x <= y + 0.02 for x, y in zip(observed, observed[1:], strict=False))
    if ece_value <= 0.05 and monotonic:
        return "good"
    return "fair" if ece_value <= 0.10 and monotonic else "poor"


# ----------------------------------------------------------------------------- folds


def quarter_starts(first: dt.date, last: dt.date) -> list[dt.date]:
    out, y, q = [], first.year, (first.month - 1) // 3
    while True:
        d = dt.date(y, q * 3 + 1, 1)
        if d > last:
            return out
        if d > first:
            out.append(d)
        q += 1
        if q == 4:
            y, q = y + 1, 0


@dataclass
class FoldResult:
    test_start: dt.date
    test_end: dt.date
    n_train: int
    n_test: int
    weight: float | None
    trained: bool


@dataclass
class WalkForward:
    rows: list[Row]
    probs: dict[int, float] = field(default_factory=dict)          # row index -> OOF model probability
    rule_cal: dict[int, float] = field(default_factory=dict)       # row index -> OOF calibrated rule prob
    weights: dict[int, float] = field(default_factory=dict)        # row index -> blend weight used
    folds: list[FoldResult] = field(default_factory=list)


def _xy(rows: Sequence[Row]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([vectorize(r.features) for r in rows], dtype=float)
    y = np.array([1 if (r.trade_return or 0) > 0 else 0 for r in rows])
    return X, y


def _fit_isotonic(scores: Sequence[float], hits: Sequence[int]):
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(np.array(scores, dtype=float), np.array(hits, dtype=float))
    return iso


def run_walk_forward(rows: list[Row], min_train: int = MIN_TRAIN) -> WalkForward:
    import lightgbm as lgb

    usable = [r for r in rows if r.trade_return is not None]
    usable.sort(key=lambda r: r.decision_date)
    wf = WalkForward(usable)
    if not usable:
        return wf
    starts = quarter_starts(usable[0].decision_date, usable[-1].decision_date)
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else usable[-1].decision_date + dt.timedelta(days=1)
        test = [i for i, r in enumerate(usable) if start <= r.decision_date < end]
        cutoff = start - dt.timedelta(days=EMBARGO_DAYS)
        train = [i for i, r in enumerate(usable)
                 if r.decision_date < cutoff and r.trade_exit_date is not None and r.trade_exit_date < start]
        if not test:
            continue
        if len(train) < min_train or len({usable[i].trade_return > 0 for i in train}) < 2:
            wf.folds.append(FoldResult(start, end, len(train), len(test), None, False))
            continue
        Xtr, ytr = _xy([usable[i] for i in train])
        model = lgb.LGBMClassifier(**LGB_PARAMS)
        model.fit(Xtr, ytr)
        Xte, _ = _xy([usable[i] for i in test])
        p = model.predict_proba(Xte)[:, 1]
        iso = _fit_isotonic([usable[i].rule_score for i in train], ytr.tolist())
        cal = iso.predict(np.array([usable[i].rule_score for i in test], dtype=float))
        # Blend weight chosen on earlier folds' out-of-sample rows only.
        prior_idx = [i for i in wf.probs if usable[i].decision_date < cutoff]
        w = _choose_weight(usable, wf, prior_idx) if prior_idx else 0.5
        for j, i in enumerate(test):
            wf.probs[i] = float(p[j])
            wf.rule_cal[i] = float(cal[j])
            wf.weights[i] = w
        wf.folds.append(FoldResult(start, end, len(train), len(test), w, True))
    return wf


def blended(r: Row, prob: float, w: float) -> float:
    return w * prob * 100 + (1 - w) * r.rule_score


def _choose_weight(rows: list[Row], wf: WalkForward, idx: list[int]) -> float:
    best, best_s = 0.5, -1e9
    for w in WEIGHTS:
        picked = [rows[i] for i in idx if blended(rows[i], wf.probs[i], w) >= DISPLAY_MIN and not rows[i].skip_reason]
        s = _trade_stats(picked).get("sharpe")
        if s is not None and s > best_s:
            best, best_s = w, s
    return best


# ----------------------------------------------------------------------------- report


def _bucket_table(rows: Sequence[Row], conf: Callable[[Row], float]) -> list[dict]:
    out = []
    for lo, hi in BUCKETS:
        sel = [r for r in rows if lo <= conf(r) < hi]
        out.append({"bucket": f"{lo}-{min(hi, 100)}", **_trade_stats(sel)})
    return out


def _by_catalyst(rows: Sequence[Row]) -> dict:
    out = {}
    for c in CATALYSTS:
        sel = [r for r in rows if r.catalyst == c]
        if sel:
            out[c] = _trade_stats(sel)
    return out


def build_report(wf: WalkForward, universe_note: str,
                 tfm_forecasts: dict[int, tuple[float, float, float]] | None = None,
                 tfm_leakage_note: str | None = None) -> dict:
    rows = wf.rows
    pos = {id(r): i for i, r in enumerate(rows)}
    oos = sorted(wf.probs)                                # rows with an out-of-sample model prediction
    oos_rows = [rows[i] for i in oos]
    shown = lambda r: r.rule_score >= DISPLAY_MIN and not r.skip_reason  # noqa: E731
    rules_rows = [r for r in oos_rows if shown(r)]
    model_rows = [rows[i] for i in oos if blended(rows[i], wf.probs[i], wf.weights[i]) >= DISPLAY_MIN
                  and not rows[i].skip_reason]
    spy = stats([r.spy_return for r in oos_rows], [r.trade_sessions or 5 for r in oos_rows])
    strategies = {
        "all_events": _trade_stats(oos_rows),
        "rules": _trade_stats(rules_rows),
        "model": _trade_stats(model_rows),
        "spy_same_days": spy,
    }
    m = strategies["model"]
    pool = [r.trade_return for r in oos_rows]
    p_model = selection_p_value([r.trade_return for r in model_rows], pool)
    p_rules = selection_p_value([r.trade_return for r in rules_rows], pool)
    strategies["model"]["p_value_vs_random_picks"] = p_model
    strategies["rules"]["p_value_vs_random_picks"] = p_rules
    model_ok = (beats(m, strategies["all_events"]) and beats(m, strategies["rules"]) and beats(m, spy)
                and m["n"] >= MIN_MODEL_TRADES and p_model is not None and p_model < SIGNIFICANCE)
    rules_ok = (beats(strategies["rules"], strategies["all_events"]) and beats(strategies["rules"], spy)
                and p_rules is not None and p_rules < SIGNIFICANCE)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "universe": universe_note,
        "rows_total": len(rows), "rows_out_of_sample": len(oos_rows),
        "period": [oos_rows[0].decision_date.isoformat(), oos_rows[-1].decision_date.isoformat()] if oos_rows else None,
        "folds": [{"test_start": f.test_start.isoformat(), "n_train": f.n_train, "n_test": f.n_test,
                   "weight": f.weight, "trained": f.trained} for f in wf.folds],
        "strategies": strategies,
        "verdict": {"model_beats_baselines": model_ok, "rules_beat_baselines": rules_ok,
                    "model_enabled": model_ok,
                    "plain": _plain(model_ok, rules_ok, strategies)},
        "by_catalyst": {"all_events": _by_catalyst(oos_rows), "rules": _by_catalyst(rules_rows)},
        "by_catalyst_full_period": _by_catalyst([r for r in rows if r.trade_return is not None]),
        "catalyst_verdicts": catalyst_verdicts(oos_rows),
        "confidence_buckets": {"rules": _bucket_table(oos_rows, lambda r: r.rule_score),
                               "model": _bucket_table(oos_rows, lambda r: blended(
                                   r, wf.probs[pos[id(r)]], wf.weights[pos[id(r)]]))},
        "priced_in_skip": {"skipped": _trade_stats([r for r in oos_rows if r.skip_reason == "priced_in"]),
                           "kept": _trade_stats([r for r in oos_rows if r.skip_reason != "priced_in"])},
        "costs": "round-trip spread + slippage by liquidity (5-60 bps each way), same as the paper account",
        "caveats": [
            "Survivorship bias: the universe is companies listed in 2026.",
            "Earnings history on Finnhub's free plan covers only the last 4 quarters per company.",
            "News headlines are not in the backtest (no licensed archive); SEC press releases stand in for them.",
        ],
    }
    if tfm_forecasts is not None:
        report["timesfm"] = _timesfm_section(rows, oos, wf, tfm_forecasts, strategies, tfm_leakage_note)
    return report


MIN_CATALYST_TRADES = 30


def catalyst_verdicts(oos_rows: Sequence[Row]) -> dict:
    """Per catalyst rule: enabled only if its out-of-sample trades beat buying the S&P 500 over the
    same days (hit rate, mean return and Sharpe). Fewer than 30 trades -> 'untested' (kept on, but
    labelled unproven; live outcomes judge it later)."""
    out = {}
    for c in CATALYSTS:
        sel = [r for r in oos_rows if r.catalyst == c and r.rule_score >= DISPLAY_MIN and not r.skip_reason]
        st = _trade_stats(sel)
        spy = stats([r.spy_return for r in sel], [r.trade_sessions or 5 for r in sel])
        if st["n"] < MIN_CATALYST_TRADES:
            status, why = "untested", f"only {st['n']} out-of-sample trades (needs {MIN_CATALYST_TRADES})"
        elif beats(st, spy) and (st["mean_pct"] or 0) > 0:
            status, why = "enabled", "beat the S&P 500 over the same days on hit rate, return and Sharpe"
        else:
            status, why = "disabled", "did not beat the S&P 500 over the same days"
        out[c] = {"status": status, "why": why, "rules": st, "spy_same_days": spy}
    return out


def timesfm_variants(rows, oos, fc: dict[int, tuple[float, float, float]]):
    """Which out-of-sample rows each variant trades. Only rows that have a forecast are compared, so all
    three variants see the same events. Returns (have, rules_only, filter, feature, feature_deltas)."""
    from catalystedge.signals.timesfm_hook import Forecast, TimesFMState, apply

    have = [i for i in oos if i in fc]
    shown = lambda i, score: score >= DISPLAY_MIN and not rows[i].skip_reason  # noqa: E731
    base_idx = [i for i in have if shown(i, rows[i].rule_score)]
    filt_idx = [i for i in base_idx if fc[i][0] > 0]
    feat_idx, deltas = [], {}
    for i in have:
        er, lo, hi = fc[i]
        deltas[i] = apply(TimesFMState(True, "feature"), Forecast("", 10, er, lo, hi, "bt", "")).confidence_delta
        if shown(i, rows[i].rule_score + deltas[i]):
            feat_idx.append(i)
    return have, base_idx, filt_idx, feat_idx, deltas


def _timesfm_section(rows, oos, wf, fc: dict[int, tuple[float, float, float]], strategies: dict,
                     leakage: str | None) -> dict:
    """fc: row index -> (expected return %, 10th pct %, 90th pct %) at the decision close."""
    have, base_idx, filt_idx, feat_idx, _ = timesfm_variants(rows, oos, fc)
    sec = {
        "rows_with_forecast": len(have),
        "rules_without_timesfm": _trade_stats([rows[i] for i in base_idx]),
        "rules_with_timesfm_filter": _trade_stats([rows[i] for i in filt_idx]),
        "rules_with_timesfm_feature": _trade_stats([rows[i] for i in feat_idx]),
        "all_events_forecast_positive": _trade_stats([rows[i] for i in have if fc[i][0] > 0]),
        "all_events_forecast_not_positive": _trade_stats([rows[i] for i in have if fc[i][0] <= 0]),
        "naive_all_events": strategies["all_events"],
        "spy_same_days": strategies["spy_same_days"],
        "leakage_warning": leakage,
    }
    # Each variant against buying the S&P 500 over exactly its own trade days (same holding periods).
    sec["spy_same_days_as"] = {}
    sec["beats_spy"] = {}
    for name, idx in (("rules_without_timesfm", base_idx), ("rules_with_timesfm_filter", filt_idx),
                      ("rules_with_timesfm_feature", feat_idx)):
        sel = [rows[i] for i in idx]
        spy = stats([r.spy_return for r in sel], [r.trade_sessions or 5 for r in sel])
        sec["spy_same_days_as"][name] = spy
        sec["beats_spy"][name] = beats(sec[name], spy) and (sec[name]["mean_pct"] or 0) > 0
    sec["filter_helps"] = beats(sec["rules_with_timesfm_filter"], sec["rules_without_timesfm"])
    sec["feature_helps"] = beats(sec["rules_with_timesfm_feature"], sec["rules_without_timesfm"])
    helps = sec["filter_helps"] or sec["feature_helps"]
    sec["helps"] = helps
    sec["plain"] = ("TimesFM improved hit rate, average return and Sharpe in this test ("
                    + ", ".join(m for m, ok in (("filter mode", sec["filter_helps"]),
                                                ("feature mode", sec["feature_helps"])) if ok) + ")."
                    if helps else "The evidence says TimesFM is NOT helping here (it did not beat the same "
                                  "rules without it on hit rate, average return and Sharpe). You can keep it on, "
                                  "but it is not earning its place.")
    return sec


def _plain(model_ok: bool, rules_ok: bool, s: dict) -> str:
    parts = []
    r, a, spy = s["rules"], s["all_events"], s["spy_same_days"]
    if r["n"]:
        parts.append(f"Rules (>= 65): {r['n']} trades, {r['hit_rate'] * 100:.0f}% winners, "
                     f"{r['mean_pct']:+.2f}% average after costs.")
    if a["n"]:
        parts.append(f"Buying every positive event: {a['hit_rate'] * 100:.0f}% winners, {a['mean_pct']:+.2f}% average.")
    if spy["n"]:
        parts.append(f"S&P 500 over the same days: {spy['hit_rate'] * 100:.0f}% up, {spy['mean_pct']:+.2f}% average.")
    m = s["model"]
    pm = m.get("p_value_vs_random_picks")
    if model_ok:
        parts.append(f"The ranking model beat every baseline ({m['n']} trades, p = {pm}) and is enabled.")
    elif m["n"] and beats(m, s["all_events"]) and beats(m, spy):
        parts.append(f"The ranking model looked better ({m['n']} trades) but not convincingly "
                     f"(p = {pm}, needs < {SIGNIFICANCE} and >= {MIN_MODEL_TRADES} trades), so it stays OFF.")
    else:
        parts.append("The ranking model did NOT beat all baselines, so it stays OFF (rules only).")
    if not rules_ok:
        parts.append("The rules themselves did not clearly beat both baselines in this test: treat signals as "
                     "ideas to research, not proven edges.")
    return " ".join(parts)


# ----------------------------------------------------------------------------- final model + calibration


def fit_final_model(rows: list[Row], path: Path, weight: float) -> dict:
    """Train on every row with a completed trade; save model + metadata for the live Ranker."""
    import lightgbm as lgb

    usable = [r for r in rows if r.trade_return is not None]
    X, y = _xy(usable)
    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(X, y)
    path.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(path / "model.txt"))
    meta = {"features": list(FEATURES), "weight": weight, "n_train": len(usable),
            "trained_at": dt.datetime.now(dt.UTC).isoformat(),
            "train_period": [usable[0].decision_date.isoformat(), usable[-1].decision_date.isoformat()],
            "label": "paper-exit trade net return > 0"}
    (path / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def calibration_snapshots(wf: WalkForward) -> list[dict]:
    """Out-of-fold reliability of the RULE confidence (score -> isotonic probability), overall
    and per catalyst family with enough rows. Each dict also carries the mapping fit on all rows
    (used live by the calibrator only when the verdict is good or fair)."""
    rows = wf.rows
    idx = sorted(wf.rule_cal)
    fams = {"all": idx}
    for c in CATALYSTS:
        sel = [i for i in idx if rows[i].catalyst == c]
        if len(sel) >= 100:
            fams[c] = sel
    out = []
    for fam, sel in fams.items():
        probs = [wf.rule_cal[i] for i in sel]
        hits = [(rows[i].trade_return or 0) > 0 for i in sel]
        e, buckets = ece(probs, hits, bins=5)
        brier = sum((p - h) ** 2 for p, h in zip(probs, hits, strict=True)) / len(sel) if sel else None
        fam_rows = [r for r in rows if r.trade_return is not None and (fam == "all" or r.catalyst == fam)]
        iso = _fit_isotonic([r.rule_score for r in fam_rows], [int(r.trade_return > 0) for r in fam_rows])
        mapping = [[float(x), float(y)] for x, y in zip(iso.X_thresholds_, iso.y_thresholds_, strict=True)]
        out.append({"event_family": fam, "n": len(sel), "ece": e, "brier": round(brier, 4) if brier else None,
                    "buckets": buckets, "verdict": verdict(e, buckets), "mapping": mapping})
    return out
