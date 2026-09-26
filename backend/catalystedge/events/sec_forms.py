"""More SEC forms, from the same official "latest filings" Atom feed as 8-K and Form 4.

Stored in `filings` (the authoritative record) and used two ways:
  * catalyst: a third-party tender offer (SC TO-T) for a company is an acquisition of that company,
    i.e. the existing `m_and_a_target` catalyst, from a primary source.
  * contradiction check: before a positive signal is shown, recent filings that cut against the story
    (a share offering, a shelf registration, a late-filing notice) are attached as risk flags.
    They do NOT change confidence or hide the signal: that filter is untested, so it stays a flag until
    it passes the backtest bar.
Everything else (13D/13G stakes, 10-Q/10-K) is context shown with the signal.

EDGAR renamed Schedules 13D/13G to "SCHEDULE 13D" / "SCHEDULE 13G" (Dec 2024); both spellings are handled.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.adapters.events import sec
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import Filing
from catalystedge.events.common import IngestReport, add_event, cik_map, ensure_ticker
from catalystedge.pipeline.ticker_link import Universe
from catalystedge.pipeline.window import cutoff

# Feed "type" is a prefix match (checked live: "S-3" also returns S-3/A and S-3ASR).
FEED_PREFIXES = ("SCHEDULE 13", "SC 13", "S-1", "S-3", "424B", "10-Q", "10-K", "NT 10", "SC TO", "SC 14D9")
MAX_PAGES = 6
CLASSIFIER = "sec-forms-v1"


@dataclass(frozen=True)
class FormKind:
    kind: str            # dilution | late_filing | activist_stake | passive_stake | tender_offer | issuer_tender |
    #                      periodic_report
    tone: str            # negative | positive | neutral (for the flag's colour; never used in scoring)
    text: str


EQUITY_TAKEDOWN = ("424B1", "424B4", "424B5", "424B7")


def classify_form(form: str) -> FormKind | None:
    f = form.upper()
    # Found live: 424B2 is almost always a bank's structured-note / debt prospectus (Citigroup, RBC, TD, ...),
    # not a share sale, and an S-3 shelf only registers POSSIBLE future sales. Only actual share-sale
    # prospectuses and S-1/F-1 offerings count as dilution.
    if f.startswith(EQUITY_TAKEDOWN) or f.startswith(("S-1", "F-1")):
        return FormKind("dilution", "negative", "prospectus or registration for selling shares (dilution risk)")
    if f.startswith("424B2"):
        return FormKind("debt_notes", "neutral", "prospectus for notes/debt (not a share sale)")
    if f.startswith(("S-3", "F-3", "424B3")):
        return FormKind("shelf", "neutral", "shelf or resale registration (possible future share sales)")
    if f.startswith("NT 10"):
        return FormKind("late_filing", "negative", "notice of late annual/quarterly report")
    if f.startswith(("SCHEDULE 13D", "SC 13D")):
        return FormKind("activist_stake", "neutral", "5%+ stake with intent to influence (13D)")
    if f.startswith(("SCHEDULE 13G", "SC 13G")):
        return FormKind("passive_stake", "neutral", "passive 5%+ stake (13G)")
    if f.startswith("SC TO-T") or f.startswith("SC 14D9"):
        return FormKind("tender_offer", "positive", "third-party tender offer for the company's shares")
    if f.startswith("SC TO-I"):
        return FormKind("issuer_tender", "neutral", "the company is buying back its own shares by tender")
    if f.startswith(("10-Q", "10-K")):
        return FormKind("periodic_report", "neutral", "new quarterly/annual report: check it matches the story")
    return None


def _subject_entries(entries: list[sec.FeedEntry]) -> list[sec.FeedEntry]:
    """One entry per filing, for the company the filing is ABOUT (Subject for 13D/TO, Filer otherwise)."""
    by_acc: dict[str, sec.FeedEntry] = {}
    for e in entries:
        if e.role in ("Subject", "Filer", "Issuer") and (e.accession not in by_acc or e.role == "Subject"):
            by_acc[e.accession] = e
    return list(by_acc.values())


def ingest_sec_forms(session: Session, http: HttpClient, ua: str, universe: Universe, now: dt.datetime
                     ) -> IngestReport:
    report = IngestReport("sec_forms")
    ciks = cik_map(universe)
    for prefix in FEED_PREFIXES:
        window: list[sec.FeedEntry] = []
        for page in range(MAX_PAGES):
            try:
                entries = sec.fetch_feed(http, ua, prefix, start=page * 100, count=100)
            except SourceError as err:
                report.errors.append(http.redact(str(err)))
                report.status = "partial"
                break
            window += [e for e in entries if cutoff(now) < e.accepted_at <= now]
            if len(entries) < 100 or min(e.accepted_at for e in entries) <= cutoff(now):
                break
        report.fetched += len(window)
        for e in _subject_entries(window):
            symbol, kind = ciks.get(e.cik), classify_form(e.form)
            if symbol is None or kind is None:
                continue
            ensure_ticker(session, symbol, universe)
            title = f"{e.form}: {kind.text}"
            res = session.execute(insert(Filing).values(
                accession=e.accession, cik=e.cik, symbol=symbol, form_type=e.form[:24], items=[],
                accepted_at=e.accepted_at, url=e.index_url, title=title).on_conflict_do_nothing()
                .returning(Filing.accession))
            report.stored += res.scalar_one_or_none() is not None
            if kind.kind == "tender_offer" and e.form.upper().startswith("SC TO-T"):
                made = add_event(session, symbol=symbol, event_type="m_and_a_target", origin="filing",
                                 headline=f"Tender offer filed for {e.company} ({e.form})", url=e.index_url,
                                 source_key="sec_edgar", available_at=e.accepted_at, polarity="positive",
                                 reasons=["third-party tender offer (SEC SC TO-T)"], materiality=0.9,
                                 classifier_version=CLASSIFIER, strength="strong", accession=e.accession,
                                 universe=universe)
                if made is not None:
                    made.verification = "primary"
                    report.events += 1
    session.flush()
    return report


def filing_flags(session: Session, symbol: str, now: dt.datetime, days: int = 30) -> list[dict]:
    """Recent SEC filings about `symbol` that a reader should weigh against a positive story, newest first.
    Includes 8-K Item 3.02 (unregistered sale of shares) from the existing 8-K ingest."""
    since = now - dt.timedelta(days=days)
    rows = session.scalars(select(Filing).where(Filing.symbol == symbol, Filing.accepted_at > since,
                                                Filing.accepted_at <= now)
                           .order_by(Filing.accepted_at.desc())).all()
    out = []
    for f in rows:
        kind = classify_form(f.form_type)
        if kind is None and f.form_type.startswith("8-K") and "3.02" in (f.items or []):
            kind = FormKind("dilution", "negative", "8-K Item 3.02: unregistered sale of shares")
        if kind is None:
            continue
        out.append({"kind": kind.kind, "tone": kind.tone, "form": f.form_type, "text": kind.text,
                    "filed_at": f.accepted_at.isoformat(), "url": f.url})
    return out


def risk_notes(flags: list[dict]) -> list[str]:
    """Plain-English notes for the contradiction check (flags only; they do not change the score)."""
    notes = []
    for kind, label in (("dilution", "Recent SEC filing to sell shares (dilution risk)"),
                        ("late_filing", "Late-filing notice (NT 10-K/10-Q) filed recently")):
        hits = [f for f in flags if f["kind"] == kind]
        if hits:
            notes.append(f"{label}: {hits[0]['form']} on {hits[0]['filed_at'][:10]}. Not yet part of the score "
                         "(untested filter).")
    return notes
