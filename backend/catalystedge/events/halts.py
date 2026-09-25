"""Trading halts from Nasdaq Trader's official halts RSS feed (covers all U.S. exchanges, including
LULD volatility pauses). Used as a risk flag on signals, never as a catalyst.

Feed items carry fields in the `ndaq:` namespace: HaltDate (MM/DD/YYYY), HaltTime (HH:MM:SS, Eastern),
IssueSymbol, IssueName, Market, ReasonCode, ResumptionDate, ResumptionTradeTime.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import TradingHalt
from catalystedge.events.common import IngestReport

FEED = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
ET_TZ = ZoneInfo("America/New_York")
REASONS = {
    "T1": "news pending", "T2": "news released", "T3": "news and resumption times", "T5": "single-stock pause",
    "T6": "extraordinary market activity", "T8": "ETF halt", "T12": "additional information requested",
    "H4": "non-compliance", "H9": "not current in filings", "H10": "SEC trading suspension",
    "H11": "regulatory concern", "LUDP": "volatility pause (limit up/limit down)",
    "LUDS": "volatility pause (straddle)", "MWC1": "market-wide circuit breaker", "M": "volatility pause",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _when(date: str | None, time: str | None) -> dt.datetime | None:
    if not date:
        return None
    try:
        d = dt.datetime.strptime(date.strip(), "%m/%d/%Y")
        t = dt.datetime.strptime((time or "00:00:00").strip()[:8], "%H:%M:%S").time()
    except ValueError:
        return None
    return dt.datetime.combine(d.date(), t, ET_TZ).astimezone(dt.UTC)


def parse_halts(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        f = {_local(c.tag): (c.text or "").strip() for c in item}
        sym = f.get("IssueSymbol", "").upper()
        halted = _when(f.get("HaltDate"), f.get("HaltTime"))
        if not sym or halted is None:
            continue
        out.append({"symbol": sym, "halted_at": halted, "reason_code": f.get("ReasonCode") or None,
                    "market": f.get("Market") or None,
                    "resumed_at": _when(f.get("ResumptionDate"), f.get("ResumptionTradeTime"))})
    return out


def ingest_halts(session: Session, http: HttpClient) -> IngestReport:
    report = IngestReport("nasdaq_halts")
    try:
        rows = parse_halts(http.get_text("nasdaq_halts", FEED, headers={"Accept": "application/rss+xml"}))
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    report.fetched = len(rows)
    for r in rows:
        stmt = insert(TradingHalt).values(**r, source_url=FEED)
        res = session.execute(stmt.on_conflict_do_update(
            constraint="uq_halts_symbol_time",
            set_={"resumed_at": stmt.excluded.resumed_at, "reason_code": stmt.excluded.reason_code},
        ).returning(TradingHalt.id))
        report.stored += res.scalar_one_or_none() is not None
    session.flush()
    return report


def halt_flags(session: Session, symbol: str, now: dt.datetime, days: int = 3) -> list[dict]:
    rows = session.scalars(select(TradingHalt).where(
        TradingHalt.symbol == symbol, TradingHalt.halted_at > now - dt.timedelta(days=days),
        TradingHalt.halted_at <= now).order_by(TradingHalt.halted_at.desc())).all()
    return [{"halted_at": h.halted_at.isoformat(), "reason_code": h.reason_code,
             "reason": REASONS.get(h.reason_code or "", h.reason_code or "unknown"),
             "resumed_at": h.resumed_at.isoformat() if h.resumed_at else None} for h in rows]


def halt_note(flags: list[dict]) -> str | None:
    if not flags:
        return None
    h = flags[0]
    state = "still halted" if not h["resumed_at"] else "resumed"
    return (f"Trading halt {h['halted_at'][:16].replace('T', ' ')} UTC ({h['reason']}), {state}"
            + (f"; {len(flags)} halts in 3 days" if len(flags) > 1 else "") + ".")
