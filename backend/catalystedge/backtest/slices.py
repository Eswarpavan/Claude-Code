"""Significance-tested slices of the out-of-sample rules trades: sector, market cap, holding period.

A slice is reported as a CANDIDATE only if all of these hold:
  - at least MIN_TRADES trades
  - it beats buying the S&P 500 over exactly the same days on win rate, average return and Sharpe,
    with a positive average
  - its average excess return over the S&P 500 is significant at p < 0.05 AFTER a Bonferroni
    correction for every slice tested (one-sided sign-flip permutation test on the paired
    per-trade excess returns; no normality assumption)
Every slice also gets the smallest average excess return it could have detected (80% power at the
corrected threshold), so "nothing passed" can be told apart from "the data could not have shown it".

Point in time only: sector comes from the SEC industry code (fixed), market cap = shares outstanding
from the latest SEC filing made before the decision date x the decision-day close, and holding
periods are fixed rules decided before entry (hold 1/3/5/10 sessions). The realised holding period
of the stop/target exit is NOT a slice: it is only known after the trade ends.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections.abc import Sequence
from pathlib import Path
from statistics import NormalDist

from catalystedge.backtest.walkforward import beats, stats
from catalystedge.signals.engine import DISPLAY_MIN

MIN_TRADES = 50
ALPHA = 0.05
N_PERM = 20000
CAP_BUCKETS = ((10e9, "Large (>= $10B)"), (2e9, "Mid ($2-10B)"), (3e8, "Small ($300M-2B)"), (0, "Micro (< $300M)"))


# ----------------------------------------------------------------------------- company facts (SEC)


def sector_of(sic: str | int | None) -> str:
    try:
        c = int(sic)
    except (TypeError, ValueError):
        return "Unknown"
    if 1300 <= c < 1400 or 2900 <= c < 3000:
        return "Energy"
    if 2830 <= c < 2840 or 3841 <= c <= 3851 or 8000 <= c < 8100 or c in (8731, 6324):
        return "Health care"
    if 6000 <= c < 6800:
        return "Financials"
    if 3570 <= c < 3580 or 3600 <= c < 3700 or 7370 <= c < 7380 or 3825 <= c <= 3829:
        return "Technology"
    if 4800 <= c < 4900 or 7810 <= c < 7820:
        return "Communication"
    if c == 3711 or 2000 <= c < 2400 or 3000 <= c < 3100 or 5000 <= c < 6000 or 7000 <= c < 8000:
        return "Consumer"
    if 1500 <= c < 1800 or 3400 <= c < 3900 or 4000 <= c < 4800:
        return "Industrials"
    return "Other"


def fetch_meta(http, ua: str, symbol: str, cik: str, cache: Path) -> dict:
    """SEC industry code and shares-outstanding history for one company (cached)."""
    path = cache / f"meta_{symbol}.json"
    if path.exists():
        return json.loads(path.read_text())
    h = {"User-Agent": ua}
    sub = http.get_json("sec_edgar", f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json", headers=h)
    shares: dict[tuple[str, str], float] = {}
    for concept in ("dei/EntityCommonStockSharesOutstanding", "us-gaap/CommonStockSharesOutstanding"):
        try:
            data = http.get_json("sec_edgar", f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik.zfill(10)}/"
                                              f"{concept}.json", headers=h)
        except Exception:
            continue
        for x in (data.get("units") or {}).get("shares", []):
            key = (x.get("filed", ""), x.get("accn", ""), x.get("end", ""))
            shares[key] = shares.get(key, 0.0) + float(x.get("val") or 0)   # share classes add up
        if shares:
            break
    meta = {"symbol": symbol, "sic": sub.get("sic"), "sic_description": sub.get("sicDescription"),
            "shares": sorted([[f, e, v] for (f, _, e), v in shares.items()])}
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta))
    return meta


def shares_at(meta: dict, day) -> float | None:
    """Shares outstanding from the latest filing made on or before `day` (point in time)."""
    known = [v for filed, _end, v in meta.get("shares", []) if filed and filed <= day.isoformat() and v > 0]
    return known[-1] if known else None


def cap_bucket(cap: float | None) -> str:
    if cap is None:
        return "Unknown"
    return next(name for lo, name in CAP_BUCKETS if cap >= lo)


def row_sector(r, meta: dict[str, dict]) -> str:
    return sector_of((meta.get(r.event.symbol) or {}).get("sic"))


def row_cap(r, meta: dict[str, dict], closes: dict) -> float | None:
    m = meta.get(r.event.symbol)
    sh = shares_at(m, r.decision_date) if m else None
    px = closes.get((r.event.symbol, r.decision_date))
    return sh * px if sh and px else None


def load_meta(cache: Path) -> dict[str, dict]:
    return {p.stem.removeprefix("meta_"): json.loads(p.read_text()) for p in cache.glob("meta_*.json")}


# ----------------------------------------------------------------------------- statistics


def sign_flip_p(excess: Sequence[float], n_perm: int = N_PERM, seed: int = 7) -> float | None:
    """One-sided: probability of an average excess this large if the trades had no edge over the S&P
    (each paired difference equally likely to be + or -)."""
    x = [v for v in excess if v is not None]
    if len(x) < 5:
        return None
    obs = statistics.mean(x)
    rnd = random.Random(seed)
    hits = sum(statistics.mean(v if rnd.random() < 0.5 else -v for v in x) >= obs for _ in range(n_perm))
    return (hits + 1) / (n_perm + 1)


def min_detectable_excess(sd: float, n: int, alpha: float, power: float = 0.8) -> float | None:
    if n < 2 or sd <= 0:
        return None
    z = NormalDist().inv_cdf(1 - alpha) + NormalDist().inv_cdf(power)
    return z * sd / math.sqrt(n)


def _test(name: str, dim: str, rets: list[float], spys: list[float], holds: list[int]) -> dict:
    pairs = [(r, s) for r, s in zip(rets, spys, strict=True) if r is not None and s is not None]
    st = stats([r for r, _ in pairs], holds[: len(pairs)] if holds else None)
    sp = stats([s for _, s in pairs], holds[: len(pairs)] if holds else None)
    ex = [r - s for r, s in pairs]
    return {"slice": name, "dimension": dim, "n": len(pairs), "strategy": st, "spy_same_days": sp,
            "mean_excess_pct": round(statistics.mean(ex), 4) if ex else None,
            "sd_excess_pct": round(statistics.pstdev(ex), 4) if len(ex) > 1 else None,
            "beats_spy": bool(pairs) and beats(st, sp) and (st["mean_pct"] or 0) > 0,
            "p_value": sign_flip_p(ex) if len(ex) >= 5 else None}


def analyse(rows: Sequence, meta: dict[str, dict], closes: dict[tuple[str, object], float]) -> dict:
    """rows: out-of-sample walk-forward rows; the rules strategy (score >= 65, not skipped) is sliced."""
    rules = [r for r in rows if r.rule_score >= DISPLAY_MIN and not r.skip_reason and r.trade_return is not None]
    tests: list[dict] = []

    def add(dim: str, key_fn):
        groups: dict[str, list] = {}
        for r in rules:
            groups.setdefault(key_fn(r), []).append(r)
        for name, sel in sorted(groups.items()):
            tests.append(_test(name, dim, [r.trade_return for r in sel], [r.spy_return for r in sel],
                               [r.trade_sessions or 5 for r in sel]))

    add("sector", lambda r: row_sector(r, meta))
    add("market cap", lambda r: cap_bucket(row_cap(r, meta, closes)))
    for h in (1, 3, 5, 10):
        sel = [r for r in rules if h in r.labels]
        tests.append(_test(f"hold exactly {h} session{'s' if h > 1 else ''}", "holding period",
                           [r.labels[h] for r in sel], [r.spy_labels.get(h) for r in sel], [h] * len(sel)))
    # Reference: insider buying held to the same bar (not a new slice dimension, but it is tested too).
    ins = [r for r in rules if r.catalyst == "insider_buy_cluster"]
    tests.append(_test("insider buying (reference)", "catalyst", [r.trade_return for r in ins],
                       [r.spy_return for r in ins], [r.trade_sessions or 5 for r in ins]))

    m = len(tests)
    alpha_corr = ALPHA / m
    for t in tests:
        t["p_bonferroni"] = min(1.0, t["p_value"] * m) if t["p_value"] is not None else None
        t["min_detectable_excess_pct"] = (round(v, 3) if (v := min_detectable_excess(
            t["sd_excess_pct"] or 0, t["n"], alpha_corr)) is not None else None)
        t["candidate"] = (t["n"] >= MIN_TRADES and t["beats_spy"] and t["p_bonferroni"] is not None
                          and t["p_bonferroni"] < ALPHA)
    big = [t for t in tests if t["n"] >= MIN_TRADES]
    overall_ex = statistics.mean([r.trade_return - r.spy_return for r in rules if r.spy_return is not None]) \
        if rules else None
    mdes = [t["min_detectable_excess_pct"] for t in big if t["min_detectable_excess_pct"] is not None]
    cands = [t["slice"] for t in tests if t["candidate"]]
    if cands:
        plain = (f"{len(cands)} slice(s) pass every test after correcting for {m} comparisons: {', '.join(cands)}. "
                 "Treat them as hypotheses to confirm on live outcomes, not as proven.")
    else:
        plain = (f"No slice passes. {len(big)} of {m} slices have at least {MIN_TRADES} trades; after correcting "
                 f"for {m} comparisons (p < {alpha_corr:.4f} each), the smallest average edge over the S&P 500 "
                 f"that any of them could reliably detect is {min(mdes):.2f}% per trade"
                 if mdes else f"No slice passes: none of the {m} slices has {MIN_TRADES} trades")
        if mdes and overall_ex is not None:
            plain += (f", while the rules as a whole beat the S&P 500 by only {overall_ex:+.2f}% per trade. "
                      "The data is too small to trust any finer slicing: a real edge of a realistic size would "
                      "not show up, and anything that looked good would most likely be noise.")
        else:
            plain += ". The data is too small to trust any slicing."
    return {"rules_trades": len(rules), "comparisons": m, "alpha_per_test": alpha_corr, "min_trades": MIN_TRADES,
            "test": "one-sided sign-flip permutation on per-trade excess return vs the S&P 500 over the same days",
            "correction": "Bonferroni", "overall_mean_excess_pct": round(overall_ex, 4) if overall_ex is not None
            else None, "candidates": cands, "slices": tests, "plain": plain}
