"""SEC 8-K / Form 4, earnings surprises, FDA approvals and trials flow into `events`
and from there through the SAME signal engine as news."""

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from catalystedge.adapters.events import sec
from catalystedge.clock import FrozenClock
from catalystedge.core import calendar
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event, Filing, InsiderTransaction, PriceDaily
from catalystedge.events.earnings import ingest_earnings, judge, report_available_at
from catalystedge.events.fda import ingest_fda, is_material
from catalystedge.events.filings import ingest_8k, ingest_form4, insider_cluster_event
from catalystedge.events.trials import ingest_trials
from catalystedge.pipeline.ticker_link import Universe
from catalystedge.signals.engine import generate_signals

UA = "Test Person test@example.com"
DAY = dt.date(2026, 9, 23)
NOW = calendar.eod_available_at(DAY) + dt.timedelta(hours=1)
U = Universe.from_sec_company_tickers({
    "0": {"cik_str": 1111111, "ticker": "ACME", "title": "Acme Robotics Inc."},
    "1": {"cik_str": 2222222, "ticker": "BIOX", "title": "Bioxcel Therapeutics, Inc."},
    "2": {"cik_str": 3333333, "ticker": "WIDG", "title": "Widget Holdings Corp"},
})


def feed(*entries):
    body = "".join(f"""<entry><title>{form} - {name} ({cik}) ({role})</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-09-23 &lt;b&gt;AccNo:&lt;/b&gt; {acc} {items}</summary>
<updated>{updated}</updated><id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"""
                   for form, name, cik, role, acc, items, updated in entries)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


INDEX_HTML = """<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/1111111/000111111126000001/acme-8k.htm">acme-8k.htm</a></td><td>8-K</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1111111/000111111126000001/ex99-1.htm">ex99-1.htm</a></td><td>EX-99.1</td></tr>
</table>"""
PRESS = """<html><body><p>Exhibit 99.1</p><p>September 23, 2026</p>
<p><b>Acme Robotics Wins $900 Million Multi-Year Contract With U.S. Army</b></p>
<p>BOSTON, Sept. 23, 2026 -- Acme Robotics (NASDAQ: ACME) today announced ...</p></body></html>"""


def form4_xml(name, code="P", shares="10000", price="25.00", plan=False, sym="WIDG"):
    return f"""<?xml version="1.0"?><ownershipDocument><aff10b5One>{1 if plan else 0}</aff10b5One>
<issuer><issuerCik>0003333333</issuerCik><issuerTradingSymbol>{sym}</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><transactionDate><value>2026-09-22</value></transactionDate>
<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
<transactionPricePerShare><value>{price}</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def client(routes):
    """routes: {(path_substring or query_marker): body}. JSON bodies may be dicts."""
    def handler(req: httpx.Request):
        url = str(req.url)
        for key, body in routes.items():
            if key in url:
                if isinstance(body, dict | list):
                    return httpx.Response(200, json=body)
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    clock = FrozenClock(NOW)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


# ----------------------------------------------------------------------------- parsing (no DB)


def test_parse_feed_extracts_items_and_acceptance_time():
    xml = feed(("8-K", "Acme Robotics Inc.", "0001111111", "Filer", "0001111111-26-000001",
                "&lt;br&gt;Item 1.01: Entry into a Material Definitive Agreement &lt;br&gt;Item 9.01: Exhibits",
                "2026-09-23T08:05:00-04:00"))
    [e] = sec.parse_feed(xml)
    assert (e.form, e.cik, e.role, e.items) == ("8-K", "0001111111", "Filer", ("1.01", "9.01"))
    assert e.accepted_at == dt.datetime(2026, 9, 23, 12, 5, tzinfo=dt.UTC)


def test_press_release_headline_skips_boilerplate():
    assert sec.headline_from_html(PRESS) == "Acme Robotics Wins $900 Million Multi-Year Contract With U.S. Army"
    assert sec.documents(INDEX_HTML)[1][0] == "EX-99.1"


def test_parse_form4_flags_plan_trades():
    f = sec.parse_form4(form4_xml("Jane Doe", plan=True))
    [t] = f.transactions
    assert (t.symbol, t.txn_code, t.shares, t.price, t.is_10b5_1) == ("WIDG", "P", 10000.0, 25.0, True)
    assert t.value_usd == 250000.0 and t.insider_role == "director"


def test_earnings_judgement():
    assert judge(12.0, 2.0)[:2] == ("positive", "earnings_beat")
    assert judge(12.0, -3.0)[:2] == ("mixed", "earnings_beat")
    assert judge(1.0, 5.0)[:2] == ("neutral", None)
    assert judge(-10.0, 0.0)[0] == "negative"
    assert report_available_at(DAY, "bmo").hour == 11            # 07:00 ET = 11:00 UTC in September
    assert report_available_at(DAY, "unknown") > report_available_at(DAY, "bmo")


def test_fda_materiality():
    assert is_material("NDA215000", {"submission_status": "AP", "submission_type": "ORIG"})
    assert is_material("BLA761000", {"submission_status": "AP", "submission_type": "SUPPL",
                                     "submission_class_code": "EFFICACY"})
    assert not is_material("ANDA210245", {"submission_status": "AP", "submission_type": "ORIG"})
    assert not is_material("NDA215000", {"submission_status": "AP", "submission_type": "SUPPL",
                                         "submission_class_code": "LABELING"})


# ----------------------------------------------------------------------------- ingestion (DB)


def _bars(db, symbol):
    for d in calendar.sessions_between(calendar.add_sessions(DAY, -70), DAY):
        db.add(PriceDaily(symbol=symbol, date=d, open=Decimal("50"), high=Decimal("51"), low=Decimal("49"),
                          close=Decimal("50.2") if d == DAY else Decimal("50"), adj_close=None, volume=2_000_000,
                          source="test", fetched_at=NOW, available_at=calendar.eod_available_at(d)))
    db.flush()


@pytest.mark.db
def test_8k_press_release_becomes_event_and_signal(db):
    http = client({
        "type=8-K": feed(("8-K", "Acme Robotics Inc.", "0001111111", "Filer", "0001111111-26-000001",
                          "&lt;br&gt;Item 8.01: Other Events", "2026-09-23T08:05:00-04:00")),
        "-index.htm": INDEX_HTML, "ex99-1.htm": PRESS})
    r = ingest_8k(db, http, UA, U, NOW)
    assert (r.stored, r.events) == (1, 1)
    ev = db.scalar(select(Event).where(Event.symbol == "ACME"))
    assert (ev.event_type, ev.polarity, ev.origin, ev.credibility) == ("contract_win", "positive", "filing", 1.0)
    assert ev.accession == "0001111111-26-000001" and "Item 8.01" in ev.headline
    assert ingest_8k(db, http, UA, U, NOW).events == 0                     # idempotent
    _bars(db, "ACME")
    c = {c.symbol: c for c in generate_signals(db, NOW).candidates}["ACME"]
    assert c.signal is not None and c.catalyst == "contract_win" and "an SEC filing" in c.signal.reason


@pytest.mark.db
def test_filings_outside_48h_are_ignored(db):
    http = client({"type=8-K": feed(("8-K", "Acme Robotics Inc.", "0001111111", "Filer", "0001111111-26-000009",
                                     "&lt;br&gt;Item 8.01: Other Events", "2026-09-20T08:05:00-04:00"))})
    assert ingest_8k(db, http, UA, U, NOW).fetched == 0


@pytest.mark.db
def test_form4_cluster_needs_two_insiders_or_100k_and_skips_plans(db):
    entries = [("4", "Widget Holdings Corp", "0003333333", "Issuer", f"0000000000-26-00000{i}", "",
                f"2026-09-23T10:0{i}:00-04:00") for i in (1, 2, 3)]
    http = client({"type=4": feed(*entries),
                   "000000000026000001/index.json": {"directory": {"item": [{"name": "f1.xml"}]}},
                   "000000000026000002/index.json": {"directory": {"item": [{"name": "f2.xml"}]}},
                   "000000000026000003/index.json": {"directory": {"item": [{"name": "f3.xml"}]}},
                   "f1.xml": form4_xml("Alice Director", shares="1000", price="20"),
                   "f2.xml": form4_xml("Bob Officer", shares="1500", price="20"),
                   "f3.xml": form4_xml("Carl Planner", shares="100000", price="20", plan=True)})
    r = ingest_form4(db, http, UA, U, NOW)
    assert r.events == 1 and db.scalar(select(Filing).where(Filing.symbol == "WIDG")) is not None
    ev = db.scalar(select(Event).where(Event.event_type == "insider_buy_cluster"))
    assert ev.symbol == "WIDG" and ev.polarity == "positive" and ev.headline.startswith("2 insiders bought $50K")
    assert "Carl" not in ev.headline                                          # 10b5-1 plan trade excluded
    assert len(db.scalars(select(InsiderTransaction)).all()) == 3


@pytest.mark.db
def test_single_small_insider_buy_is_not_a_cluster(db):
    from catalystedge.events.common import ensure_ticker

    ensure_ticker(db, "WIDG", U)
    db.add(Filing(accession="0000000000-26-000099", cik="0003333333", symbol="WIDG", form_type="4", items=[],
                  accepted_at=NOW - dt.timedelta(hours=3), url="u"))
    db.add(InsiderTransaction(accession="0000000000-26-000099", symbol="WIDG", insider_name="Solo", txn_code="P",
                              acquired_disposed="A", shares=Decimal("100"), price=Decimal("20"),
                              value_usd=Decimal("2000"), txn_date=DAY, available_at=NOW - dt.timedelta(hours=3)))
    db.flush()
    assert insider_cluster_event(db, "WIDG", NOW, U) is None


@pytest.mark.db
def test_earnings_surprise_events(db):
    http = client({"calendar/earnings": {"earningsCalendar": [
        {"symbol": "ACME", "date": "2026-09-23", "hour": "bmo", "quarter": 3, "year": 2026, "epsEstimate": 1.00,
         "epsActual": 1.20, "revenueEstimate": 1e9, "revenueActual": 1.05e9},
        {"symbol": "BIOX", "date": "2026-09-23", "hour": "amc", "quarter": 3, "year": 2026, "epsEstimate": 0.50,
         "epsActual": 0.60, "revenueEstimate": 1e8, "revenueActual": 0.9e8},
        {"symbol": "WIDG", "date": "2026-09-25", "hour": "bmo", "quarter": 3, "year": 2026, "epsEstimate": 0.3,
         "epsActual": None, "revenueEstimate": None, "revenueActual": None},
        {"symbol": "ZZZZ", "date": "2026-09-23", "hour": "bmo", "quarter": 3, "year": 2026, "epsEstimate": 1,
         "epsActual": 2, "revenueEstimate": 1, "revenueActual": 2}]}})
    r = ingest_earnings(db, http, "key", U, NOW)
    assert (r.fetched, r.stored, r.events) == (3, 3, 2)
    evs = {e.symbol: e for e in db.scalars(select(Event).where(Event.origin == "earnings"))}
    assert (evs["ACME"].event_type, evs["ACME"].polarity) == ("earnings_beat", "positive")
    assert evs["BIOX"].polarity == "mixed"                                    # revenue miss
    assert "+20.0%" in evs["ACME"].headline
    assert ingest_earnings(db, http, "key", U, NOW).events == 0


@pytest.mark.db
def test_earnings_without_key_is_disabled(db):
    assert ingest_earnings(db, client({}), None, U, NOW).status == "disabled"


@pytest.mark.db
def test_fda_approval_event(db):
    http = client({"drugsfda.json": {"results": [
        {"application_number": "NDA219999", "sponsor_name": "BIOXCEL THERAP", "products": [{"brand_name": "NOVADRUG"}],
         "submissions": [{"submission_type": "ORIG", "submission_status": "AP", "submission_status_date": "20260922"}]},
        {"application_number": "ANDA200000", "sponsor_name": "GENERIC CO", "products": [],
         "submissions": [{"submission_type": "ORIG", "submission_status": "AP", "submission_status_date": "20260922"}]},
    ]}})
    r = ingest_fda(db, http, U, NOW)
    assert r.events == 1
    ev = db.scalar(select(Event).where(Event.origin == "fda"))
    assert (ev.symbol, ev.event_type, ev.polarity) == ("BIOX", "fda_approval", "positive") and "NOVADRUG" in ev.headline


@pytest.mark.db
def test_trials_blocked_is_reported_not_raised(db):
    def handler(req):
        raise httpx.ConnectError("Tunnel connection failed: 403 Forbidden")
    clock = FrozenClock(NOW)
    http = HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)
    r = ingest_trials(db, http, U, NOW)
    assert r.status in ("blocked", "failed") and r.errors


def _buy(db, name, role="director", value=150_000, acc="0000000000-26-000100", hours=3):
    from catalystedge.events.common import ensure_ticker

    ensure_ticker(db, "WIDG", U)
    if db.get(Filing, acc) is None:
        db.add(Filing(accession=acc, cik="0003333333", symbol="WIDG", form_type="4", items=[],
                      accepted_at=NOW - dt.timedelta(hours=hours), url="u"))
        db.flush()
    db.add(InsiderTransaction(accession=acc, symbol="WIDG", insider_name=name, insider_role=role, txn_code="P",
                              acquired_disposed="A", shares=Decimal("1000"), price=Decimal(str(value / 1000)),
                              value_usd=Decimal(str(value)), txn_date=DAY, available_at=NOW - dt.timedelta(hours=hours)))
    db.flush()


@pytest.mark.db
def test_funds_and_ten_percent_owners_are_not_insider_buyers(db):
    _buy(db, "ORBIMED ADVISORS LLC", role="director, 10% owner", value=5_000_000)
    _buy(db, "Big Holder", role="10% owner", value=5_000_000)
    assert insider_cluster_event(db, "WIDG", NOW, U) is None


@pytest.mark.db
def test_private_placement_week_is_not_an_insider_signal(db):
    _buy(db, "Jane Director", value=200_000)
    db.add(Filing(accession="0003333333-26-000001", cik="0003333333", symbol="WIDG", form_type="8-K",
                  items=["3.02", "9.01"], accepted_at=NOW - dt.timedelta(days=2), url="u"))
    db.flush()
    assert insider_cluster_event(db, "WIDG", NOW, U) is None


@pytest.mark.db
def test_single_insider_cluster_is_weak(db):
    _buy(db, "Jane Director", value=200_000)
    ev = insider_cluster_event(db, "WIDG", NOW, U)
    assert ev is not None and ev.strength == "weak"



@pytest.mark.db
def test_older_private_placement_found_via_submissions_is_excluded(db):
    _buy(db, "Jane Director", value=200_000)
    assert insider_cluster_event(db, "WIDG", NOW, U, had_private_placement=lambda s: True) is None
    assert insider_cluster_event(db, "WIDG", NOW, U, had_private_placement=lambda s: False) is not None


def test_recent_offering_or_ipo_counts_as_financing():
    subs = {"filings": {"recent": {"form": ["4", "S-1MEF", "S-1"], "filingDate": ["2026-09-22", "2026-09-17",
            "2026-08-28"], "items": ["", "", ""], "accessionNumber": ["a-1", "b-2", "c-3"]}, "files": []}}
    assert sec.had_recent_financing(client({"submissions": subs}), UA, "3333333", dt.date(2026, 9, 10))
    quiet = {"filings": {"recent": {"form": ["4", "10-Q"], "filingDate": ["2026-09-22", "2026-08-01"],
             "items": ["", ""], "accessionNumber": ["a-1", "b-2"]}, "files": []}}
    assert not sec.had_recent_financing(client({"submissions": quiet}), UA, "3333333", dt.date(2026, 9, 10))
