"""Cached EDGAR / earnings / FDA history -> HistEvent list, using the live rules.

8-K headlines use classify.py (same rules and model veto policy as live); Form 4
clusters use the same person/financing/threshold rules as events/filings.py; earnings
surprises are timed by the company's own 8-K Item 2.02 acceptance time.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

from catalystedge.backtest.dataset import HistEvent
from catalystedge.backtest.history import load_company
from catalystedge.events.earnings import judge, surprise_pct
from catalystedge.events.filings import CLUSTER_DAYS, CLUSTER_MIN_INSIDERS, CLUSTER_MIN_USD, is_insider_person
from catalystedge.ml.sentiment import SentimentModel
from catalystedge.pipeline.classify import classify
from catalystedge.pipeline.ticker_link import Mention, Universe, link_tickers
from catalystedge.signals.priors import PRIORS

FINANCING_LOOKBACK = dt.timedelta(days=14)


def _ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return None


def eight_k_events(records: list[dict], universe: Universe, model: SentimentModel | None) -> list[HistEvent]:
    recs = [r for r in records if r.get("form") == "8-K" and r.get("headline") and not r.get("offering")]
    scores = model.predict([r["headline"] for r in recs]) if (model and recs) else [None] * len(recs)
    out = []
    for r, score in zip(recs, scores, strict=True):
        sym, headline, at = r["symbol"], r["headline"], _ts(r["accepted_at"])
        if at is None:
            continue
        link = link_tickers(headline, {sym: 1.0}, universe)
        mentions = [m for m in link.mentions if m.symbol == sym] or [Mention(sym, "filing", 0.99, is_primary=True)]
        mentions += [m for m in link.mentions if m.symbol != sym]
        for ev in classify(headline, mentions, score):
            if ev.symbol == sym and ev.is_signal_eligible and ev.event_type in PRIORS:
                out.append(HistEvent(sym, ev.event_type, "filing", f"8-K: {headline}", at,
                                     materiality=ev.materiality or 0.6, credibility=1.0, strength=ev.strength,
                                     sentiment=ev.sentiment, source_key="sec_edgar", ref=r["accession"]))
    return out


def insider_events(records: list[dict]) -> list[HistEvent]:
    """Replay the live cluster rule at each qualifying Form 4's acceptance time."""
    financings = sorted(t for r in records if r.get("offering") and (t := _ts(r["accepted_at"])) is not None)
    buys = []   # (available_at, txn_date, name, value, accession)
    for r in records:
        if r.get("form") != "4" or (at := _ts(r.get("accepted_at", ""))) is None:
            continue
        for t in r.get("transactions") or []:
            if (t["txn_code"] == "P" and t["acquired_disposed"] == "A" and not t["is_10b5_1"]
                    and is_insider_person(t["insider_name"], t.get("insider_role")) and t.get("price")):
                buys.append((at, dt.date.fromisoformat(t["txn_date"]), t["insider_name"],
                             t["shares"] * t["price"], r["accession"]))
    buys.sort()
    out: list[HistEvent] = []
    last_event: dt.datetime | None = None
    for at, _, _, _, acc in buys:
        if last_event is not None and at - last_event < dt.timedelta(days=CLUSTER_DAYS):
            continue                                   # one cluster per 10-day window
        window = [b for b in buys if b[0] <= at and b[1] >= (at - dt.timedelta(days=CLUSTER_DAYS)).date()]
        by_name: dict[str, float] = defaultdict(float)
        for b in window:
            by_name[b[2]] += b[3]
        n, total = len(by_name), sum(by_name.values())
        if n < CLUSTER_MIN_INSIDERS and total < CLUSTER_MIN_USD:
            continue
        if any(at - FINANCING_LOOKBACK <= f <= at for f in financings):
            continue
        mat = min(0.95, max(0.5, 0.6 + 0.15 * math.log10(max(total, 1e4) / 1e5) + 0.05 * (n - 1)))
        sym = records[0]["symbol"]
        out.append(HistEvent(sym, "insider_buy_cluster", "filing",
                             f"{n} insider(s) bought ${total / 1e3:,.0f}K of {sym} (Form 4)", at, materiality=mat,
                             credibility=1.0, strength="strong" if n >= 3 else ("normal" if n == 2 else "weak"),
                             source_key="sec_edgar", ref=acc))
        last_event = at
    return out


def earnings_events(symbol: str, finnhub_rows: list[dict], records: list[dict]) -> list[HistEvent]:
    """Finnhub actual vs estimate per fiscal period, timed by the first 8-K Item 2.02 accepted
    after the period end (within 75 days). No 8-K match -> no event (never guess the time)."""
    results_8k = sorted(t for r in records if r.get("form") == "8-K" and "2.02" in (r.get("items") or [])
                        and (t := _ts(r["accepted_at"])) is not None)
    out = []
    for row in finnhub_rows:
        period = row.get("period")
        if not period:
            continue
        end = dt.datetime.fromisoformat(period).replace(tzinfo=dt.UTC)
        at = next((t for t in results_8k if end < t <= end + dt.timedelta(days=75)), None)
        if at is None:
            continue
        eps_s = surprise_pct(row.get("actual"), row.get("estimate"))
        polarity, etype, reasons = judge(eps_s, None)
        if polarity != "positive" or etype != "earnings_beat":
            continue
        out.append(HistEvent(symbol, "earnings_beat", "earnings",
                             f"{symbol} {period} EPS {row.get('actual')} vs {row.get('estimate')} est ({eps_s:+.1f}%)",
                             at, materiality=min(0.95, 0.6 + max(0.0, eps_s) / 50), credibility=1.0,
                             strength="strong" if eps_s >= 15 else "normal", source_key="finnhub_earnings",
                             ref=f"{symbol}:{period}"))
    return out


def fda_events(rows: list[dict]) -> list[HistEvent]:
    out = []
    for r in rows:
        at = _ts(r["available_at"])
        if at:
            out.append(HistEvent(r["symbol"], "fda_approval", "fda", r["headline"], at, materiality=r["materiality"],
                                 credibility=1.0, source_key="openfda", ref=r["ref"]))
    return out


def load_events(cache_dir: Path, symbols: list[str], universe: Universe, model: SentimentModel | None
                ) -> list[HistEvent]:
    events: list[HistEvent] = []
    for sym in symbols:
        recs = load_company(cache_dir, sym)
        if not recs:
            continue
        events += eight_k_events(recs, universe, model)
        events += insider_events(recs)
        ef = cache_dir / f"earnings_{sym}.json"
        if ef.exists():
            events += earnings_events(sym, json.loads(ef.read_text()), recs)
    ff = cache_dir / "fda.json"
    if ff.exists():
        events += [e for e in fda_events(json.loads(ff.read_text())) if e.symbol in set(symbols)]
    return events
