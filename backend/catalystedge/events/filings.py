"""SEC filings -> events.

8-K: items 1.01 (material agreement), 2.02 (results), 7.01 / 8.01 (press releases).
     The EX-99 press-release headline goes through the SAME classifier as news
     (classify.py), with the filer as the subject. Credibility 1.0 (primary source).
Form 4: open-market purchases (code P, acquired) that are not 10b5-1 plan trades, by
     directors or officers (funds, holding companies and pure 10% owners excluded: their
     buys are usually financings or portfolio moves). A cluster (>= 2 distinct insiders,
     or >= $100k, within 10 days) becomes an `insider_buy_cluster` event (ARCHITECTURE
     §9.1.4). Skipped when the issuer filed an 8-K Item 3.02 (unregistered sale of
     equity, i.e. a private placement) in the same 10 days. One insider alone is "weak".
Only filings accepted inside the 48-hour window are used for live signals.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.adapters.events import sec
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import Filing, InsiderTransaction
from catalystedge.events.common import IngestReport, add_event, cik_map, ensure_ticker
from catalystedge.ml.sentiment import SentimentModel
from catalystedge.pipeline.classify import CLASSIFIER_VERSION, classify
from catalystedge.pipeline.ticker_link import Mention, Universe, link_tickers
from catalystedge.pipeline.window import cutoff

EIGHT_K_ITEMS = {"1.01", "2.02", "7.01", "8.01"}
CLUSTER_DAYS = 10
CLUSTER_MIN_INSIDERS = 2
CLUSTER_MIN_USD = 100_000.0
MAX_FEED_PAGES = 12
PLACEMENT_LOOKBACK_DAYS = 14
ENTITY = re.compile(r"\b(?:llc|l\.?p\.?|lp|inc|corp|fund|funds|partners|advisors|advisers|capital|holdings?|trust|"
                    r"management|ventures|group|ltd|limited|plc|foundation|investments?|company|co)\b\.?", re.I)


def is_insider_person(name: str, role: str | None) -> bool:
    """A director or officer who is a person (not a fund/company, not only a 10% owner)."""
    r = (role or "").lower()
    return not ENTITY.search(name) and bool(r) and r != "10% owner"


def _feed_window(http: HttpClient, ua: str, form: str, now: dt.datetime, report: IngestReport) -> list[sec.FeedEntry]:
    out: list[sec.FeedEntry] = []
    for page in range(MAX_FEED_PAGES):
        try:
            entries = sec.fetch_feed(http, ua, form, start=page * 100, count=100)
        except SourceError as e:
            report.errors.append(http.redact(str(e)))
            report.status = "partial" if out else "failed"
            break
        out += [e for e in entries if cutoff(now) < e.accepted_at <= now]
        if not entries or min(e.accepted_at for e in entries) <= cutoff(now):
            break
    report.fetched += len(out)
    return out


def _store_filing(session: Session, e: sec.FeedEntry, symbol: str | None, title: str | None) -> bool:
    res = session.execute(insert(Filing).values(
        accession=e.accession, cik=e.cik, symbol=symbol, form_type=e.form, items=list(e.items),
        accepted_at=e.accepted_at, url=e.index_url, title=title).on_conflict_do_nothing().returning(Filing.accession))
    return res.scalar_one_or_none() is not None


def ingest_8k(session: Session, http: HttpClient, ua: str, universe: Universe, now: dt.datetime,
              model: SentimentModel | None = None) -> IngestReport:
    report = IngestReport("sec_8k")
    ciks = cik_map(universe)
    for e in _feed_window(http, ua, "8-K", now, report):
        symbol = ciks.get(e.cik)
        if e.role != "Filer" or symbol is None or session.get(Filing, e.accession) is not None:
            continue
        headline = None
        if set(e.items) & EIGHT_K_ITEMS:
            try:
                headline = sec.press_release_headline(http, ua, e)
            except SourceError as err:
                report.errors.append(http.redact(str(err)))
        ensure_ticker(session, symbol, universe)
        report.stored += _store_filing(session, e, symbol, headline)
        if not headline:
            continue
        link = link_tickers(headline, {symbol: 1.0}, universe)
        mentions = [m for m in link.mentions if m.symbol == symbol] or [Mention(symbol, "filing", 0.99,
                                                                                 is_primary=True)]
        others = [m for m in link.mentions if m.symbol != symbol]
        score = model.predict([headline])[0] if model else None
        for ev in classify(headline, mentions + others, score):
            if ev.symbol != symbol:
                continue
            made = add_event(
                session, symbol=symbol, event_type=ev.event_type, origin="filing",
                headline=f"8-K (Item {', '.join(e.items)}): {headline}", url=e.index_url, source_key="sec_edgar",
                available_at=e.accepted_at, polarity=ev.polarity, reasons=list(ev.reasons),
                materiality=ev.materiality or 0.6, classifier_version=CLASSIFIER_VERSION, strength=ev.strength,
                sentiment=ev.sentiment, mixed=ev.mixed_resolution, accession=e.accession, universe=universe)
            report.events += made is not None
    session.flush()
    return report


def ingest_form4(session: Session, http: HttpClient, ua: str, universe: Universe, now: dt.datetime) -> IngestReport:
    report = IngestReport("sec_form4")
    ciks = cik_map(universe)
    touched: set[str] = set()
    for e in _feed_window(http, ua, "4", now, report):
        if e.role != "Issuer" or session.get(Filing, e.accession) is not None:
            continue
        symbol = ciks.get(e.cik)
        if symbol is None:
            continue
        try:
            f4 = sec.fetch_form4(http, ua, e)
        except SourceError as err:
            report.errors.append(http.redact(str(err)))
            continue
        ensure_ticker(session, symbol, universe)
        report.stored += _store_filing(session, e, symbol, f"Form 4: {e.company}")
        if f4 is None:
            continue
        for t in f4.transactions:
            session.add(InsiderTransaction(
                accession=e.accession, symbol=symbol, insider_name=t.insider_name, insider_role=t.insider_role,
                txn_code=t.txn_code, acquired_disposed=t.acquired_disposed, shares=Decimal(str(t.shares)),
                price=Decimal(str(t.price)) if t.price is not None else None,
                value_usd=Decimal(str(round(t.value_usd, 2))) if t.value_usd is not None else None,
                txn_date=t.txn_date, available_at=e.accepted_at, is_10b5_1=t.is_10b5_1))
            if t.txn_code == "P" and t.acquired_disposed == "A" and not t.is_10b5_1:
                touched.add(symbol)
    session.flush()
    for symbol in sorted(touched):
        c = universe.by_symbol.get(symbol)

        def placement_check(sym: str, cik: str | None = c.cik if c else None) -> bool:
            if not cik:
                return False
            try:
                since = (now - dt.timedelta(days=PLACEMENT_LOOKBACK_DAYS)).date()
                return sec.had_recent_financing(http, ua, cik, since)
            except SourceError as err:
                report.errors.append(http.redact(str(err)))
                return False

        report.events += insider_cluster_event(session, symbol, now, universe, placement_check) is not None
    return report


def insider_cluster_event(session: Session, symbol: str, now: dt.datetime, universe: Universe | None = None,
                          had_private_placement: Callable[[str], bool] | None = None):
    """Create an insider_buy_cluster event if open-market buys in the last 10 days qualify."""
    since = (now - dt.timedelta(days=CLUSTER_DAYS)).date()
    rows = session.execute(select(
        InsiderTransaction.insider_name, func.sum(InsiderTransaction.value_usd),
        func.max(InsiderTransaction.available_at), func.max(InsiderTransaction.accession)).where(
        InsiderTransaction.symbol == symbol, InsiderTransaction.txn_code == "P",
        InsiderTransaction.acquired_disposed == "A", InsiderTransaction.is_10b5_1.is_(False),
        InsiderTransaction.txn_date >= since, InsiderTransaction.available_at <= now)
        .group_by(InsiderTransaction.insider_name, InsiderTransaction.insider_role)
        .add_columns(InsiderTransaction.insider_role)).all()
    rows = [r for r in rows if is_insider_person(r[0], r[4])]
    if not rows:
        return None
    placement = session.scalar(select(Filing.accession).where(
        Filing.symbol == symbol, Filing.form_type.in_(("8-K", "8-K/A")), Filing.items.any("3.02"),
        Filing.accepted_at >= now - dt.timedelta(days=CLUSTER_DAYS), Filing.accepted_at <= now).limit(1))
    if placement is not None or (had_private_placement is not None and had_private_placement(symbol)):
        return None
    n = len(rows)
    total = sum(float(r[1] or 0) for r in rows)
    if n < CLUSTER_MIN_INSIDERS and total < CLUSTER_MIN_USD:
        return None
    latest_at = max(r[2] for r in rows)
    latest_acc = max(rows, key=lambda r: r[2])[3]
    mat = min(0.95, 0.6 + 0.15 * math.log10(max(total, 1e4) / 1e5) + 0.05 * (n - 1))
    who = ", ".join(sorted(r[0] for r in rows)[:3]) + (" and others" if n > 3 else "")
    headline = (f"{n} insider{'s' if n > 1 else ''} bought ${total / 1e3:,.0f}K of {symbol} in the open market "
                f"in the last {CLUSTER_DAYS} days (Form 4: {who})")
    return add_event(
        session, symbol=symbol, event_type="insider_buy_cluster", origin="filing", headline=headline,
        url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&CIK={symbol}",
        source_key="sec_edgar", available_at=latest_at, polarity="positive",
        reasons=[f"{n} insiders, ${total:,.0f}, excluding 10b5-1 plan trades"], materiality=round(max(0.5, mat), 3),
        classifier_version="form4-cluster-v2", strength="strong" if n >= 3 else ("normal" if n == 2 else "weak"),
        accession=latest_acc,
        universe=universe)
