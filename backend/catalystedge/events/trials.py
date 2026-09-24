"""ClinicalTrials.gov v2 -> `trial_events` (enrichment only, never a signal on its own).

A registry change ("results posted", "completed", "terminated") says nothing about
whether the result was good, so it is stored for the hover card and as a model
feature. Positive trial results reach signals through news and 8-K press releases,
which the classifier reads ("met its primary endpoint").
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import TrialEvent
from catalystedge.events.common import IngestReport
from catalystedge.pipeline.ticker_link import Universe, link_tickers

ET = ZoneInfo("America/New_York")
URL = "https://clinicaltrials.gov/api/v2/studies"


def ingest_trials(session: Session, http: HttpClient, universe: Universe, now: dt.datetime,
                  days_back: int = 2) -> IngestReport:
    report = IngestReport("clinicaltrials")
    start = (now - dt.timedelta(days=days_back)).date().isoformat()
    try:
        data = http.get_json("clinicaltrials", URL, {
            "filter.advanced": f"AREA[ResultsFirstPostDate]RANGE[{start},MAX]", "pageSize": 100,
            "fields": "NCTId,LeadSponsorName,OverallStatus,ResultsFirstPostDate,BriefTitle"})
    except SourceError as e:
        msg = str(e)
        report.status = "blocked" if ("403" in msg or "Tunnel" in msg or "connect" in msg.lower()) else "failed"
        report.errors.append(http.redact(msg))
        return report
    for study in data.get("studies") or []:
        ps = study.get("protocolSection", {})
        nct = ps.get("identificationModule", {}).get("nctId")
        sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
        posted = (study.get("resultsSection") and ps.get("statusModule", {}).get("resultsFirstPostDateStruct", {})
                  .get("date"))
        if not nct or not posted:
            continue
        report.fetched += 1
        day = dt.date.fromisoformat(posted[:10])
        symbol = link_tickers(sponsor, {}, universe).primary
        session.execute(insert(TrialEvent).values(
            symbol=symbol, nct_id=nct, change="results_posted", change_date=day,
            available_at=dt.datetime.combine(day, dt.time(23, 59), ET).astimezone(dt.UTC),
            url=f"https://clinicaltrials.gov/study/{nct}").on_conflict_do_nothing(constraint="uq_trial_event"))
        report.stored += 1
    session.flush()
    return report
