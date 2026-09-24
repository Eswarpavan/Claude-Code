"""Earnings surprises (Finnhub earnings calendar: actual vs consensus) -> events.

EPS beat of >= 3% (and >= $0.01) with revenue in line or better -> `earnings_beat`
(positive). EPS beat with a revenue miss of more than 1% -> mixed (never a signal).
Misses -> negative. `available_at`: before-open reports at 07:00 ET, after-close and
unknown timing at 16:15 ET of the report date (the conservative, later reading).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import Earnings
from catalystedge.events.common import IngestReport, add_event
from catalystedge.pipeline.ticker_link import Universe

ET = ZoneInfo("America/New_York")
URL = "https://finnhub.io/api/v1/calendar/earnings"
EPS_BEAT_PCT = 3.0
REV_MISS_PCT = -1.0


def report_available_at(day: dt.date, hour: str | None) -> dt.datetime:
    t = dt.time(7, 0) if hour == "bmo" else dt.time(16, 15)
    return dt.datetime.combine(day, t, ET).astimezone(dt.UTC)


def surprise_pct(actual: float | None, estimate: float | None) -> float | None:
    if actual is None or estimate is None or abs(estimate) < 0.01:
        return None
    return (actual - estimate) / abs(estimate) * 100


def judge(eps_s: float | None, rev_s: float | None) -> tuple[str, str | None, list[str]]:
    """(polarity, event_type or None, reasons)."""
    if eps_s is None:
        return "neutral", None, ["no usable EPS estimate"]
    if eps_s >= EPS_BEAT_PCT:
        if rev_s is not None and rev_s < REV_MISS_PCT:
            return "mixed", "earnings_beat", [f"EPS beat {eps_s:+.1f}% but revenue missed {rev_s:+.1f}%"]
        return "positive", "earnings_beat", [f"EPS beat {eps_s:+.1f}%" + (f", revenue {rev_s:+.1f}%" if rev_s
                                                                            is not None else "")]
    if eps_s <= -EPS_BEAT_PCT:
        return "negative", "other", [f"EPS miss {eps_s:+.1f}%"]
    return "neutral", None, [f"EPS in line ({eps_s:+.1f}%)"]


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"${x / 1e9:.2f}B" if abs(x) >= 1e9 else f"${x / 1e6:.0f}M"


def ingest_earnings(session: Session, http: HttpClient, api_key: str | None, universe: Universe,
                    now: dt.datetime, days_back: int = 3) -> IngestReport:
    report = IngestReport("finnhub_earnings")
    if not api_key:
        report.status = "disabled"
        report.errors.append("FINNHUB_API_KEY not set")
        return report
    start = (now - dt.timedelta(days=days_back)).date()
    try:
        data = http.get_json("finnhub_earnings", URL, {"from": start.isoformat(), "to": now.date().isoformat(),
                                                       "token": api_key})
    except SourceError as e:
        report.status = "failed"
        report.errors.append(http.redact(str(e)))
        return report
    for r in data.get("earningsCalendar") or []:
        symbol = str(r.get("symbol") or "").upper().replace("-", ".")
        if symbol not in universe.by_symbol or not r.get("date"):
            continue
        report.fetched += 1
        day = dt.date.fromisoformat(r["date"])
        hour = r.get("hour") if r.get("hour") in ("bmo", "amc") else "unknown"
        at = report_available_at(day, hour)
        eps_a, eps_e = r.get("epsActual"), r.get("epsEstimate")
        rev_a, rev_e = r.get("revenueActual"), r.get("revenueEstimate")
        eps_s, rev_s = surprise_pct(eps_a, eps_e), surprise_pct(rev_a, rev_e)
        period = f"{r.get('year')}Q{r.get('quarter')}"
        vals = dict(report_date=day, timing=hour, eps_est=_dec(eps_e), eps_actual=_dec(eps_a), rev_est=_dec(rev_e),
                    rev_actual=_dec(rev_a), surprise_pct=eps_s, available_at=at if eps_a is not None else None)
        stmt = insert(Earnings).values(symbol=symbol, fiscal_period=period, source="finnhub", **vals)
        session.execute(stmt.on_conflict_do_update(index_elements=["symbol", "fiscal_period", "source"],
                                                   set_=vals))
        report.stored += 1
        if eps_a is None or at > now:
            continue
        polarity, etype, reasons = judge(eps_s, rev_s)
        if etype is None:
            continue
        headline = (f"{symbol} {period} EPS ${eps_a:.2f} vs ${eps_e:.2f} est ({eps_s:+.1f}%), revenue "
                    f"{_money(rev_a)} vs {_money(rev_e)} est" + (f" ({rev_s:+.1f}%)" if rev_s is not None else ""))
        mat = min(0.95, 0.6 + max(0.0, eps_s) / 50) if eps_s else 0.6
        made = add_event(
            session, symbol=symbol, event_type=etype, origin="earnings", headline=headline,
            url=f"https://finnhub.io/docs/api/earnings-calendar#{symbol}", source_key="finnhub_earnings",
            available_at=at, polarity=polarity, reasons=reasons, materiality=round(mat, 3),
            classifier_version="earnings-v1",
            strength="strong" if (eps_s or 0) >= 15 and (rev_s or 0) > 0 else "normal",
            mixed={"conflict": reasons[0]} if polarity == "mixed" else None,
            earnings_key={"symbol": symbol, "period": period, "source": "finnhub"}, universe=universe)
        report.events += made is not None
    session.flush()
    return report


def _dec(x: float | None) -> Decimal | None:
    return Decimal(str(x)) if x is not None else None
