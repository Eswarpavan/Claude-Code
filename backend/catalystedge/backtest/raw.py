"""Raw per-trade backtest results, saved into the repository so the exact numbers survive a restart.

    catalystedge backtest --timesfm --raw-out ../docs/backtest     # writes the files below
    catalystedge backtest-summary ../docs/backtest/trades.csv       # recomputes every table from the CSV

Files:
  trades.csv        one line per backtest row: every out-of-sample row (an empty return means the
                    trade had not finished when the data ended) and every in-sample row with a
                    completed trade, with the flags that say which strategy traded it
  report.json       the full report of that run
  ../BACKTEST_RAW.md  the summary tables (recomputed from trades.csv) and the out-of-sample trade list
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path

from catalystedge.backtest.walkforward import blended, stats, timesfm_variants
from catalystedge.signals.engine import DISPLAY_MIN

COLUMNS = [
    "row", "out_of_sample", "symbol", "catalyst", "origin", "event_ref", "headline", "available_at",
    "decision_date", "entry_date", "entry_price", "exit_date", "exit_reason", "sessions",
    "sentiment_model", "sentiment_pos", "sentiment_neg", "rule_score", "skip_reason",
    "model_prob", "model_weight", "model_confidence",
    "timesfm_expected_pct", "timesfm_low_pct", "timesfm_high_pct", "timesfm_feature_delta",
    "in_rules", "in_naive_all_events", "in_rules_plus_model",
    "tfm_compared", "in_rules_without_timesfm", "in_timesfm_filter", "in_timesfm_feature",
    "cost_pct", "trade_return_pct", "spy_return_pct",
    "sector", "market_cap_usd", "cap_bucket",
    *[f"hold{h}_pct" for h in (1, 3, 5, 10)], *[f"spy_hold{h}_pct" for h in (1, 3, 5, 10)],
]

VARIANTS = (("Rules + FinBERT (TimesFM off)", "in_rules_without_timesfm"),
            ("Rules + FinBERT + TimesFM feature mode", "in_timesfm_feature"),
            ("Rules + FinBERT + TimesFM filter mode", "in_timesfm_filter"))
STRATEGIES = (("Rules (confidence >= 65)", "in_rules"), ("Naive: every positive event", "in_naive_all_events"),
              ("Rules + LightGBM", "in_rules_plus_model"))


def _r(x, nd=4):
    return "" if x is None else round(float(x), nd)


def trade_rows(wf, fc: dict | None, row_ctx: dict | None = None) -> list[dict]:
    rows, oos = wf.rows, sorted(wf.probs)
    oos_set = set(oos)
    have, base, filt, feat, deltas = timesfm_variants(rows, oos, fc) if fc else ([], [], [], [], {})
    have, base, filt, feat = set(have), set(base), set(filt), set(feat)
    out = []
    for i, r in enumerate(rows):
        o = i in oos_set
        if r.trade_return is None and not o:
            continue
        conf = blended(r, wf.probs[i], wf.weights[i]) if o else None
        senti = r.event.sentiment or {}
        f = (fc or {}).get(i)
        out.append({
            "row": i, "out_of_sample": int(o), "symbol": r.event.symbol, "catalyst": r.catalyst,
            "origin": r.event.origin, "event_ref": r.event.ref, "headline": r.event.headline[:160],
            "available_at": r.event.available_at.isoformat(), "decision_date": r.decision_date.isoformat(),
            "entry_date": r.entry_date.isoformat(), "entry_price": _r(r.entry_price),
            "exit_date": r.trade_exit_date.isoformat() if r.trade_exit_date else "",
            "exit_reason": r.trade_exit_reason or "time_stop", "sessions": r.trade_sessions or "",
            "sentiment_model": senti.get("model", ""), "sentiment_pos": _r(senti.get("pos"), 3),
            "sentiment_neg": _r(senti.get("neg"), 3), "rule_score": _r(r.rule_score, 2),
            "skip_reason": r.skip_reason or "",
            "model_prob": _r(wf.probs.get(i), 4), "model_weight": _r(wf.weights.get(i), 3),
            "model_confidence": _r(conf, 2),
            "timesfm_expected_pct": _r(f[0]) if f else "", "timesfm_low_pct": _r(f[1]) if f else "",
            "timesfm_high_pct": _r(f[2]) if f else "", "timesfm_feature_delta": _r(deltas.get(i), 3),
            "in_rules": int(o and r.rule_score >= DISPLAY_MIN and not r.skip_reason),
            "in_naive_all_events": int(o),
            "in_rules_plus_model": int(o and conf is not None and conf >= DISPLAY_MIN and not r.skip_reason),
            "tfm_compared": int(i in have), "in_rules_without_timesfm": int(i in base),
            "in_timesfm_filter": int(i in filt), "in_timesfm_feature": int(i in feat),
            "cost_pct": _r(r.cost_pct),
            # full precision, so every statistic recomputed from the CSV matches the report exactly
            "trade_return_pct": "" if r.trade_return is None else float(r.trade_return),
            "spy_return_pct": "" if r.spy_return is None else float(r.spy_return),
            **{k: (row_ctx or {}).get(i, {}).get(k, "") for k in ("sector", "market_cap_usd", "cap_bucket")},
            **{f"hold{h}_pct": r.labels.get(h, "") for h in (1, 3, 5, 10)},
            **{f"spy_hold{h}_pct": r.spy_labels.get(h, "") for h in (1, 3, 5, 10)},
        })
    return out


def write_csv(rows: Sequence[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _num(x):
    return float(x) if x not in ("", None) else None


def summarize(rows: Sequence[dict], flag: str) -> dict:
    """The strategy's stats and the S&P 500 over exactly the same trades' days (same formula as the report)."""
    sel = [r for r in rows if str(r[flag]) == "1"]
    holds = [int(r["sessions"] or 5) for r in sel]
    return {"strategy": stats([_num(r["trade_return_pct"]) for r in sel], holds),
            "spy_same_days": stats([_num(r["spy_return_pct"]) for r in sel], holds)}


def _fmt(st: dict) -> tuple[str, str, str, str]:
    if not st["n"]:
        return "0", "–", "–", "–"
    sharpe = f"{st['sharpe']:.2f}" if st["sharpe"] is not None else "–"
    return str(st["n"]), f"{st['hit_rate'] * 100:.1f}%", f"{st['mean_pct']:+.2f}%", sharpe


def _table(rows: Sequence[dict], items) -> list[str]:
    out = ["| Row | Trades | Win rate | Avg return / trade | Sharpe "
           "| S&P 500, same days: win · avg · Sharpe | Beat S&P? |",
           "|---|---|---|---|---|---|---|"]
    for name, flag in items:
        s = summarize(rows, flag)
        n, hit, mean, sh = _fmt(s["strategy"])
        _, shit, smean, ssh = _fmt(s["spy_same_days"])
        st, sp = s["strategy"], s["spy_same_days"]
        beat = ("–" if not st["n"] else
                "yes" if all(st[k] is not None and sp[k] is not None and st[k] > sp[k]
                             for k in ("hit_rate", "mean_pct", "sharpe")) and st["mean_pct"] > 0 else "no")
        out.append(f"| {name} | {n} | {hit} | {mean} | {sh} | {shit} · {smean} · {ssh} | {beat} |")
    return out


def markdown(rows: Sequence[dict], report: dict, csv_rel: str, json_rel: str) -> str:
    oos = [r for r in rows if r["out_of_sample"] in (1, "1") and r["trade_return_pct"] not in ("", None)]
    dates = sorted(r["decision_date"] for r in oos)
    models = sorted({r["sentiment_model"] for r in rows if r["sentiment_model"]})
    tfm = report.get("timesfm") or {}
    lines = [
        "# Backtest: raw results",
        "",
        f"Generated by `catalystedge backtest --timesfm --raw-out` on {report.get('generated_at', '')[:10]}. "
        f"Every number below is recomputed from [`{csv_rel}`]({csv_rel}) "
        f"(`catalystedge backtest-summary {csv_rel}`), so nothing needs re-downloading. Full report: "
        f"[`{json_rel}`]({json_rel}).",
        "",
        f"- Universe: {report.get('note', '')}",
        f"- Out-of-sample decisions: {dates[0] if dates else '–'} to {dates[-1] if dates else '–'} "
        f"({len(oos)} completed trades); walk-forward with a 14-day embargo, no lookahead.",
        "- Costs: round-trip spread + slippage by liquidity (5–60 bps each way), same as the paper account; "
        "every return below is after costs.",
        f"- Sentiment on headlines: {', '.join(models) or 'none'}. "
        "Sharpe is annualised from the average holding period.",
        "- \"Beat S&P?\" = higher win rate, higher average return and higher Sharpe than buying the S&P 500 "
        "over exactly the same holding days, with a positive average.",
        "- Ollama is not part of the backtest: it only writes explanation text after scoring and never "
        "changes confidence, polarity, catalyst status or which trades are taken.",
        "",
        "## TimesFM: the three variants",
        "",
        "Compared on the same out-of-sample events (those with a TimesFM forecast).",
        "",
        *_table(rows, VARIANTS),
        "",
    ]
    if tfm.get("leakage_warning"):
        lines += [f"> Caveat: {tfm['leakage_warning']}", ""]
    lines += ["## All strategies (out of sample)", "", *_table(rows, STRATEGIES), ""]
    lines += _shap_md(report.get("shap") or {}) + _slices_md(report.get("slices") or {})
    lines += [
              "## Out-of-sample trades", "",
              "`R` = rules trade, `F` = TimesFM filter keeps it, `T` = TimesFM feature mode trades it.", "",
              "| Decision | Symbol | Catalyst | Score | TimesFM fc | Flags | Return | S&P same days | Exit |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(oos, key=lambda x: (x["decision_date"], x["symbol"])):
        flags = "".join(c for c, k in (("R", "in_rules_without_timesfm"), ("F", "in_timesfm_filter"),
                                       ("T", "in_timesfm_feature")) if str(r[k]) == "1") or "·"
        fc = f"{float(r['timesfm_expected_pct']):+.2f}%" if r["timesfm_expected_pct"] not in ("", None) else "–"
        spy = f"{float(r['spy_return_pct']):+.2f}%" if r["spy_return_pct"] not in ("", None) else "–"
        lines.append(f"| {r['decision_date']} | {r['symbol']} | {r['catalyst']} | {float(r['rule_score']):.0f} | "
                     f"{fc} | {flags} | {float(r['trade_return_pct']):+.2f}% | {spy} | {r['exit_reason']} |")
    return "\n".join(lines) + "\n"


def _shap_md(sh: dict) -> list[str]:
    if not sh.get("features"):
        return []
    out = ["## What the LightGBM model weighted (SHAP)", "",
           f"Mean absolute SHAP value per feature on {sh['n_rows']} out-of-sample rows (each quarter's model "
           f"scored rows it had not trained on), in {sh.get('units', 'log-odds')}. The model is **disabled**: "
           "it did not beat the baselines, so these weights are shown for transparency only.", "",
           "| Feature | Meaning | Mean abs SHAP | Share of total | Average direction |", "|---|---|---|---|---|"]
    for f in sh["features"]:
        d = "raises win odds" if f["mean_signed_shap"] > 0 else "lowers win odds"
        out.append(f"| `{f['feature']}` | {f['plain']} | {f['mean_abs_shap']:.4f} | {f['share_pct']:.1f}% | {d} |")
    if sh.get("unused_features"):
        out += ["", "Never used by any fold's model: " + ", ".join(f"`{x}`" for x in sh["unused_features"]) + "."]
    return out + [""]


def _slices_md(sl: dict) -> list[str]:
    if not sl.get("slices"):
        return ["## Slices", "", sl.get("plain", "not run"), ""]
    out = ["## Slices by sector, market cap and holding period", "",
           f"Rules trades out of sample: {sl['rules_trades']}. Bar for a candidate: at least {sl['min_trades']} "
           f"trades, beats the S&P 500 over the same days on win rate, average return and Sharpe, and "
           f"p < 0.05 after a {sl['correction']} correction for {sl['comparisons']} comparisons "
           f"(p < {sl['alpha_per_test']:.4f} each; {sl['test']}).", "",
           f"**Verdict: {sl['plain']}**", "",
           "| Dimension | Slice | Trades | Win rate | Avg / trade | Sharpe | S&P same days avg | Excess | p | "
           "p (corrected) | Smallest detectable excess | Candidate |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in sl["slices"]:
        n, hit, mean, sh = _fmt(t["strategy"])
        sp = t["spy_same_days"]
        spm = f"{sp['mean_pct']:+.2f}%" if sp["n"] else "–"
        ex = f"{t['mean_excess_pct']:+.2f}%" if t["mean_excess_pct"] is not None else "–"
        p = f"{t['p_value']:.3f}" if t["p_value"] is not None else "–"
        pc = f"{t['p_bonferroni']:.3f}" if t["p_bonferroni"] is not None else "–"
        mde = f"{t['min_detectable_excess_pct']:.2f}%" if t["min_detectable_excess_pct"] is not None else "–"
        flag = "**yes**" if t["candidate"] else ("no" if t["n"] >= sl["min_trades"] else f"no (< {sl['min_trades']})")
        out.append(f"| {t['dimension']} | {t['slice']} | {n} | {hit} | {mean} | {sh} | {spm} | {ex} | {p} | {pc} | "
                   f"{mde} | {flag} |")
    out += ["", "The realised holding period of each stop/target exit is not a slice: it is only known after "
            "the trade ends, so filtering on it would use the future. The holding-period rows instead apply a "
            "fixed hold, decided before entry, to the same rules entries.", ""]
    return out


def export(wf, fc: dict | None, report: dict, out_dir: Path, md_path: Path,
           row_ctx: dict | None = None) -> dict[str, Path]:
    rows = trade_rows(wf, fc, row_ctx)
    csv_path, json_path = out_dir / "trades.csv", out_dir / "report.json"
    write_csv(rows, csv_path)
    json_path.write_text(json.dumps(report, indent=1, default=str))
    rel = lambda p: str(p.resolve().relative_to(md_path.parent.resolve()))  # noqa: E731
    md_path.write_text(markdown(read_csv(csv_path), report, rel(csv_path), rel(json_path)))
    return {"csv": csv_path, "json": json_path, "md": md_path}
