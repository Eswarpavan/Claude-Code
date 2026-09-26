"""Government contract awards from official sources, as primary-source `contract_win` events.

  dod_contracts   Defense Department daily contract announcements (official RSS, awards >= $7.5M, published
                  around 5 pm Eastern on business days). Timely.
  usaspending     USAspending.gov award search API (free, no key). Authoritative but LAGS: agencies report
                  late and DoD data is delayed ~90 days, so it confirms awards rather than catching them early.
  sam_gov         SAM.gov Contract Opportunities API, award notices (free api.data.gov key: SAM_GOV_API_KEY).

Materiality: big contractors win awards every day. An award becomes a `contract_win` catalyst only when it is
at least MIN_SHARE_OF_CAP of the company's market cap (when the market cap is known); otherwise it is stored
as neutral context (`other`) so it cannot flood the signals.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

from sqlalchemy.orm import Session

from catalystedge.adapters.events.sec import _Text
from catalystedge.adapters.news.rss import _date, ensure_feed
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import Ticker
from catalystedge.events.common import IngestReport, add_event
from catalystedge.pipeline.ticker_link import Universe, clean_name
from catalystedge.pipeline.window import cutoff

# The Defense Department site moved to war.gov (defense.gov redirects there, checked live 2026-09-26).
DOD_FEED = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"
USASPENDING = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
SAM = "https://api.sam.gov/opportunities/v2/search"
MIN_SHARE_OF_CAP = 0.01
MIN_AMOUNT = 25e6
CLASSIFIER = "gov-contracts-v1"

_AWARD = re.compile(
    r"^(?P<company>[A-Z0-9][^,]{1,80}?(?:,\s*(?:Inc|LLC|L\.L\.C|Corp|Co|Ltd|L\.P|LP)\.?)?),\s+"
    r"(?P<place>[^$]{2,120}?)\s*,?\s+(?:is|was|has been|have been|were|are)\s+(?:each\s+)?(?:being\s+)?"
    r"(?:awarded|issued|given)\b[^$]{0,120}?\$(?P<amount>[\d,]+(?:\.\d+)?)(?:\s*(?P<unit>million|billion))?",
    re.S)


def _amount(num: str, unit: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * (1e9 if unit == "billion" else 1e6 if unit == "million" else 1)


def parse_dod_item(text: str) -> list[dict]:
    """Awards from one day's announcement text: one paragraph per award."""
    out = []
    for para in (p.strip() for p in text.split("\n")):
        m = _AWARD.match(para)
        if m:
            out.append({"company": m["company"].strip(), "amount": _amount(m["amount"], m["unit"]),
                        "text": para[:300]})
    return out


def parse_dod_feed(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        published = _date(it.findtext("pubDate"))
        p = _Text()
        p.feed(it.findtext("description") or "")
        body = "".join(p.parts)                         # read in memory for the award lines; never stored
        for award in parse_dod_item(body):
            items.append({**award, "url": link, "published": published})
    return items


def company_symbol(name: str, universe: Universe) -> str | None:
    """Conservative contractor -> ticker link. The listed company's name must START the contractor's name, and a
    one-word company name must BE the whole contractor name: "Duluth Travel Inc" is not Duluth Holdings (found
    live), while "General Dynamics Information Technology" is General Dynamics. Some true links are missed
    (e.g. "V2X Systems"), never invented."""
    cleaned = clean_name(name.upper())
    words = cleaned.lower().split()
    for sym, start, _end, alias in universe.alias_matches(cleaned):
        if start != 0:
            continue
        alias_words = alias.lower().split()
        if len(alias_words) == 1 and words != alias_words:
            continue
        resolved = universe.resolve(sym)
        if resolved:
            return resolved
    return None


def _record(session: Session, universe: Universe, report: IngestReport, *, company: str, amount: float, url: str,
            at: dt.datetime, source_key: str, agency: str) -> None:
    symbol = company_symbol(company, universe)
    if symbol is None or amount < MIN_AMOUNT:
        return
    t = session.get(Ticker, symbol)
    cap = float(t.market_cap) if t is not None and t.market_cap else None
    material = cap is not None and amount >= MIN_SHARE_OF_CAP * cap
    share = f" ({amount / cap:.1%} of market cap)" if cap else " (market cap unknown: stored as context)"
    headline = f"{company} awarded ${amount / 1e6:,.0f} million {agency} contract{share}"
    made = add_event(session, symbol=symbol, event_type="contract_win" if material else "other", origin="gov",
                     headline=headline, url=url, source_key=source_key, available_at=at,
                     polarity="positive" if material else "neutral",
                     reasons=[f"official {agency} award", "material vs market cap" if material else "not material"],
                     materiality=0.8 if material else 0.3, classifier_version=CLASSIFIER,
                     strength="normal", universe=universe)
    report.events += made is not None and material
    report.stored += made is not None


def ingest_dod(session: Session, http: HttpClient, universe: Universe, now: dt.datetime) -> IngestReport:
    report = IngestReport("dod_contracts")
    try:
        awards = parse_dod_feed(ensure_feed(http.get_text("dod_contracts", DOD_FEED,
                                                          headers={"Accept": "application/rss+xml"}), "dod_contracts"))
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    report.fetched = len(awards)
    for a in awards:
        at = a["published"]
        if at is None or not (cutoff(now) < at <= now):
            continue
        _record(session, universe, report, company=a["company"], amount=a["amount"], url=a["url"], at=at,
                source_key="dod_contracts", agency="Defense Department")
    session.flush()
    return report


def ingest_usaspending(session: Session, http: HttpClient, universe: Universe, now: dt.datetime,
                       days: int = 7) -> IngestReport:
    """Large contract awards with an action date in the last `days`. available_at = when WE saw them (the
    publication time is unknown and usually weeks after the award), so this never pretends to be early."""
    report = IngestReport("usaspending")
    body = {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                        # New awards only: an action-date search also returns old contracts that were merely
                        # modified this week, with their lifetime totals (found live: a $43B lab contract).
                        "time_period": [{"start_date": (now.date() - dt.timedelta(days=days)).isoformat(),
                                         "end_date": now.date().isoformat(), "date_type": "new_awards_only"}],
                        "award_amounts": [{"lower_bound": MIN_AMOUNT}]},
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "generated_internal_id"],
            "sort": "Award Amount", "order": "desc", "limit": 100, "page": 1}
    try:
        data = http.post_json("usaspending", USASPENDING, body)
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    rows = data.get("results") or []
    report.fetched = len(rows)
    for r in rows:
        url = f"https://www.usaspending.gov/award/{r.get('generated_internal_id', '')}"
        _record(session, universe, report, company=r.get("Recipient Name") or "", amount=float(r.get("Award Amount")
                                                                                               or 0),
                url=url, at=now, source_key="usaspending", agency=r.get("Awarding Agency") or "federal")
    session.flush()
    return report


def ingest_sam(session: Session, http: HttpClient, api_key: str | None, universe: Universe,
               now: dt.datetime) -> IngestReport:
    report = IngestReport("sam_gov")
    if not api_key:
        report.status, report.errors = "disabled", ["auth required: set SAM_GOV_API_KEY (free at api.data.gov)"]
        return report
    params = {"api_key": api_key, "ptype": "a", "limit": 100,
              "postedFrom": cutoff(now).date().strftime("%m/%d/%Y"),
              "postedTo": now.date().strftime("%m/%d/%Y")}
    try:
        data = http.get_json("sam_gov", SAM, params)
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    rows = data.get("opportunitiesData") or []
    report.fetched = len(rows)
    for r in rows:
        award = r.get("award") or {}
        awardee = (award.get("awardee") or {}).get("name") or ""
        try:
            amount = float(str(award.get("amount") or 0).replace(",", ""))
        except ValueError:
            continue
        at = _date(r.get("postedDate")) or now
        _record(session, universe, report, company=awardee, amount=amount, url=r.get("uiLink") or SAM,
                at=min(at, now), source_key="sam_gov", agency=r.get("fullParentPathName", "federal").split(".")[0])
    session.flush()
    return report
