"""FDA approvals (openFDA drugsfda) -> `fda_approval` events.

Counted: NDA/BLA (not ANDA generics) with an ORIGINAL approval, or an efficacy
supplement (new indication). Labeling/manufacturing supplements are ignored.
The sponsor name is linked to a ticker with the same linker as news, so an
unrecognisable sponsor (private company, abbreviation) links to nothing.

Timing (rule 7): openFDA publishes weekly, often days after the action. The event
is `available_at` 16:15 ET on the action date (public that day via FDA and the
company), but only if our fetch happened after that. Because of the lag, most
openFDA approvals are older than 48 h when they appear and so never make live
signals; the same approval usually arrives first as news or an 8-K.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import FdaEvent
from catalystedge.events.common import IngestReport, add_event
from catalystedge.pipeline.ticker_link import Universe, clean_name, link_tickers

ET = ZoneInfo("America/New_York")
URL = "https://api.fda.gov/drug/drugsfda.json"


def is_material(app_no: str, sub: dict) -> bool:
    if not app_no.upper().startswith(("NDA", "BLA")) or sub.get("submission_status") != "AP":
        return False
    return sub.get("submission_type") == "ORIG" or (sub.get("submission_class_code") or "").upper() == "EFFICACY"


def sponsor_symbol(sponsor: str, universe: Universe) -> str | None:
    name = clean_name(sponsor.replace("PHARMS", "PHARMACEUTICALS").replace("THERAP", "THERAPEUTICS"))
    link = link_tickers(name, {}, universe)
    return link.primary


def ingest_fda(session: Session, http: HttpClient, universe: Universe, now: dt.datetime,
               days_back: int = 14) -> IngestReport:
    report = IngestReport("openfda")
    start = (now - dt.timedelta(days=days_back)).strftime("%Y%m%d")
    try:
        data = http.get_json("openfda", URL, {
            "search": f"submissions.submission_status_date:[{start} TO {now:%Y%m%d}] AND "
                      "submissions.submission_status:AP", "limit": 100})
    except SourceError as e:
        report.status = "blocked" if "403" in str(e) or "Tunnel" in str(e) else "failed"
        report.errors.append(http.redact(str(e)))
        return report
    for r in data.get("results") or []:
        app_no = r.get("application_number") or ""
        sponsor = r.get("sponsor_name") or ""
        product = ", ".join(sorted({p.get("brand_name") for p in r.get("products", []) if p.get("brand_name")}))
        for sub in r.get("submissions") or []:
            date = sub.get("submission_status_date") or ""
            if not date or date < start or not is_material(app_no, sub):
                continue
            report.fetched += 1
            day = dt.datetime.strptime(date, "%Y%m%d").date()
            at = dt.datetime.combine(day, dt.time(16, 15), ET).astimezone(dt.UTC)
            if at > now:
                continue
            symbol = sponsor_symbol(sponsor, universe)
            action = "original approval" if sub.get("submission_type") == "ORIG" else "new indication (efficacy)"
            url = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_no[3:]}"
            stmt = insert(FdaEvent).values(symbol=symbol, application_no=app_no, product=product, action=action,
                                           action_date=day, available_at=at, url=url)
            session.execute(stmt.on_conflict_do_nothing(constraint="uq_fda_event"))
            fe = session.scalar(select(FdaEvent).where(FdaEvent.application_no == app_no, FdaEvent.action == action,
                                                       FdaEvent.action_date == day))
            report.stored += 1
            if symbol is None or fe is None:
                continue
            made = add_event(
                session, symbol=symbol, event_type="fda_approval", origin="fda",
                headline=f"FDA {action}: {product or app_no} ({sponsor.title()}, {app_no})", url=url,
                source_key="openfda", available_at=at, polarity="positive",
                reasons=[f"openFDA {app_no} {sub.get('submission_type')} approved {day.isoformat()}"],
                materiality=0.85 if sub.get("submission_type") == "ORIG" else 0.7, classifier_version="openfda-v1",
                fda_event_id=fe.id, universe=universe)
            report.events += made is not None
    session.flush()
    return report
