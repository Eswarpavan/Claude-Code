"""Run every non-news event source once. Each failure is contained and reported."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.events.common import IngestReport
from catalystedge.events.earnings import ingest_earnings
from catalystedge.events.fda import ingest_fda
from catalystedge.events.filings import ingest_8k, ingest_form4
from catalystedge.events.halts import ingest_halts
from catalystedge.events.sec_forms import ingest_sec_forms
from catalystedge.events.trials import ingest_trials
from catalystedge.ml.sentiment import SentimentModel
from catalystedge.pipeline.ticker_link import Universe


def ingest_all(session: Session, http: HttpClient, settings: Settings, universe: Universe, now: dt.datetime,
               model: SentimentModel | None = None) -> list[IngestReport]:
    reports: list[IngestReport] = []
    ua = settings.sec_user_agent

    def run(name: str, fn) -> None:
        try:
            reports.append(fn())
        except Exception as e:  # one broken source never stops the others
            r = IngestReport(name, status="failed")
            r.errors.append(http.redact(f"{type(e).__name__}: {e}"))
            reports.append(r)

    if ua:
        run("sec_8k", lambda: ingest_8k(session, http, ua, universe, now, model))
        run("sec_form4", lambda: ingest_form4(session, http, ua, universe, now))
        run("sec_forms", lambda: ingest_sec_forms(session, http, ua, universe, now))
    else:
        reports += [IngestReport(n, status="disabled", errors=["SEC_USER_AGENT not set"])
                    for n in ("sec_8k", "sec_form4", "sec_forms")]
    run("finnhub_earnings", lambda: ingest_earnings(session, http, settings.finnhub_api_key, universe, now))
    run("openfda", lambda: ingest_fda(session, http, universe, now))
    run("clinicaltrials", lambda: ingest_trials(session, http, universe, now))
    run("nasdaq_halts", lambda: ingest_halts(session, http))
    return reports
