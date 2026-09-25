"""Macro context: market-wide releases and the calendar of upcoming ones. Context only, never a stock catalyst.

  federal_reserve  official RSS: press releases (FOMC statements) and speeches
  bls              official "latest releases" RSS (CPI, PPI, jobs, unemployment, ...)
  fred             FRED release calendar (free key FRED_API_KEY): upcoming dates for CPI, the jobs report, GDP,
                   PCE, PPI, retail sales, jobless claims and FOMC statements. ISM is a private survey with no
                   free official feed, so it is not included (manual only).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.adapters.news.rss import parse_feed
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import MacroRelease
from catalystedge.events.common import IngestReport
from catalystedge.pipeline.window import cutoff

ET = ZoneInfo("America/New_York")
FED_FEEDS = {"press": "https://www.federalreserve.gov/feeds/press_all.xml",
             "speech": "https://www.federalreserve.gov/feeds/speeches.xml"}
BLS_FEED = "https://www.bls.gov/feed/bls_latest.rss"
FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"
# FRED release ids of the market-moving U.S. releases.
FRED_RELEASES = {10: "CPI", 50: "Jobs report (Employment Situation)", 53: "GDP",
                 54: "PCE (Personal Income and Outlays)", 46: "PPI", 9: "Retail sales", 180: "Jobless claims",
                 101: "FOMC statement"}


def _store(session: Session, report: IngestReport, *, source: str, title: str, url: str, at: dt.datetime,
           kind: str, data: dict | None = None) -> None:
    res = session.execute(insert(MacroRelease).values(source=source, title=title[:500], url=url, published_at=at,
                                                      kind=kind, data=data).on_conflict_do_nothing()
                          .returning(MacroRelease.id))
    report.stored += res.scalar_one_or_none() is not None


def ingest_fed_bls(session: Session, http: HttpClient, now: dt.datetime) -> IngestReport:
    report = IngestReport("macro_feeds")
    feeds = [("federal_reserve", kind, url) for kind, url in FED_FEEDS.items()] + [("bls", "release", BLS_FEED)]
    for source, kind, url in feeds:
        try:
            items = parse_feed(http.get_text(source, url, headers={"Accept": "application/rss+xml"}))
        except SourceError as e:
            report.status = "partial"
            report.errors.append(http.redact(str(e)))
            continue
        for it in items:
            if it.published and now - dt.timedelta(days=14) < it.published <= now:
                report.fetched += 1
                _store(session, report, source=source, title=it.title, url=it.link, at=it.published, kind=kind)
    session.flush()
    return report


def ingest_fred_calendar(session: Session, http: HttpClient, api_key: str | None, now: dt.datetime,
                         days_ahead: int = 14) -> IngestReport:
    report = IngestReport("fred_calendar")
    if not api_key:
        report.status, report.errors = "disabled", ["auth required: set FRED_API_KEY (free at fred.stlouisfed.org)"]
        return report
    today = now.astimezone(ET).date()
    try:
        data = http.get_json("fred", FRED_DATES, {
            "api_key": api_key, "file_type": "json", "realtime_start": today.isoformat(),
            "realtime_end": (today + dt.timedelta(days=days_ahead)).isoformat(),
            "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 1000})
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    for r in data.get("release_dates") or []:
        rid = int(r.get("release_id", 0))
        if rid not in FRED_RELEASES:
            continue
        day = dt.date.fromisoformat(r["date"])
        report.fetched += 1
        # FRED gives the date, not the time; most of these print at 8:30 ET (FOMC at 14:00 ET).
        hour, minute = (14, 0) if rid == 101 else (8, 30)
        at = dt.datetime.combine(day, dt.time(hour, minute), ET).astimezone(dt.UTC)
        _store(session, report, source="fred", title=FRED_RELEASES[rid],
               url=f"https://fred.stlouisfed.org/releases/calendar?rid={rid}&date={day}", at=at, kind="scheduled",
               data={"release_id": rid, "time_is_approximate": True})
    session.flush()
    return report


def macro_context(session: Session, now: dt.datetime) -> dict:
    """What happened in the last 2 days and what is scheduled in the next 7 (for the dashboard)."""
    recent = session.scalars(select(MacroRelease).where(
        MacroRelease.kind != "scheduled", MacroRelease.published_at > cutoff(now),
        MacroRelease.published_at <= now).order_by(MacroRelease.published_at.desc()).limit(10)).all()
    upcoming = session.scalars(select(MacroRelease).where(
        MacroRelease.kind == "scheduled", MacroRelease.published_at > now - dt.timedelta(hours=12),
        MacroRelease.published_at <= now + dt.timedelta(days=7)).order_by(MacroRelease.published_at)).all()

    def j(m: MacroRelease) -> dict:
        return {"source": m.source, "title": m.title, "url": m.url, "at": m.published_at.isoformat(), "kind": m.kind}

    return {"recent": [j(m) for m in recent], "upcoming": [j(m) for m in upcoming],
            "note": "Market-wide context only; never a stock catalyst. Scheduled times are approximate "
                    "(most U.S. data prints at 8:30 ET, FOMC statements at 14:00 ET)."}
