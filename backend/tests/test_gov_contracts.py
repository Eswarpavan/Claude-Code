"""Government contract awards: official sources, materiality vs market cap, no key leaks."""

import datetime as dt
import json
from decimal import Decimal

import httpx
from sqlalchemy import select

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event, Ticker
from catalystedge.events.common import ensure_ticker
from catalystedge.events.gov_contracts import (
    ingest_dod,
    ingest_sam,
    ingest_usaspending,
    parse_dod_feed,
    parse_dod_item,
)
from tests.test_event_sources import U

NOW = dt.datetime(2026, 9, 23, 23, 0, tzinfo=dt.UTC)
DOD = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Contracts For Sept. 23, 2026</title>
<link>https://www.defense.gov/News/Contracts/Contract/Article/1/</link><pubDate>Wed, 23 Sep 2026 21:00:00 GMT</pubDate>
<description><![CDATA[<p>ARMY</p>
<p>Acme Robotics Inc., San Jose, California, was awarded a $950,000,000 firm-fixed-price contract.</p>
<p>Widget Holdings Corp., Dayton, Ohio, has been awarded a $30,000,000 modification (W56-26-C-0001) for spare parts.</p>
<p>Unlisted Services LLC, Norfolk, Virginia, is awarded a $400,000,000 contract for ship repair.</p>]]></description>
</item></channel></rss>"""


def client(handler):
    clock = FrozenClock(NOW)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def test_parse_dod_awards():
    [a, w, u] = parse_dod_feed(DOD)
    assert (a["company"], a["amount"]) == ("Acme Robotics Inc.", 950e6)
    assert (w["company"], w["amount"]) == ("Widget Holdings Corp.", 30e6)
    assert u["company"] == "Unlisted Services LLC"
    assert parse_dod_item("Lockheed Martin Corp., Grand Prairie, Texas, is being awarded a $1.2 billion contract.")[0][
        "amount"] == 1.2e9


def _caps(db, **caps):
    for sym, cap in caps.items():
        ensure_ticker(db, sym, U)
        db.get(Ticker, sym).market_cap = Decimal(str(cap))
    db.flush()


def test_material_awards_become_contract_wins_others_context(db):
    _caps(db, ACME=20e9, WIDG=90e9)                 # $950M = 4.75% of ACME; $30M = 0.03% of WIDG
    rep = ingest_dod(db, client(lambda r: httpx.Response(200, text=DOD)), U, NOW)
    evs = {e.symbol: e for e in db.scalars(select(Event))}
    assert evs["ACME"].event_type == "contract_win" and evs["ACME"].origin == "gov"
    assert evs["ACME"].verification == "primary" and "4.8% of market cap" in evs["ACME"].headline
    assert evs["WIDG"].event_type == "other" and evs["WIDG"].polarity == "neutral"
    assert rep.events == 1 and "Unlisted" not in " ".join(e.headline for e in evs.values())


def test_usaspending_posts_a_search_and_marks_it_seen_now(db):
    _caps(db, ACME=20e9)
    sent = []

    def h(req):
        sent.append((req.method, json.loads(req.content)))
        return httpx.Response(200, json={"results": [{"Recipient Name": "ACME ROBOTICS INC", "Award Amount": 5e8,
                                                      "Awarding Agency": "Department of Energy",
                                                      "generated_internal_id": "CONT_AWD_X"}]})

    ingest_usaspending(db, client(h), U, NOW)
    method, body = sent[0]
    assert method == "POST" and body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
    [ev] = db.scalars(select(Event)).all()
    assert ev.available_at == NOW and ev.event_type == "contract_win" and ev.url.endswith("CONT_AWD_X")


def test_sam_needs_a_key_and_never_leaks_it(db):
    assert ingest_sam(db, client(lambda r: httpx.Response(500)), None, U, NOW).status == "disabled"
    rep = ingest_sam(db, client(lambda r: httpx.Response(403, text=f"bad key {r.url}")), "SAMKEY123", U, NOW)
    assert rep.status == "failed" and "SAMKEY123" not in " ".join(rep.errors)


def test_contractor_linking_is_conservative():
    """Found live: 'Duluth Travel Inc' (a travel agency) was linked to Duluth Holdings (a clothing retailer)."""
    from catalystedge.events.gov_contracts import company_symbol
    from catalystedge.pipeline.ticker_link import Universe

    u = Universe.from_sec_company_tickers({
        "0": {"cik_str": 1, "ticker": "DLTH", "title": "Duluth Holdings Inc."},
        "1": {"cik_str": 2, "ticker": "GD", "title": "GENERAL DYNAMICS CORP"},
    })
    assert company_symbol("DULUTH TRAVEL INC", u) is None
    assert company_symbol("GENERAL DYNAMICS INFORMATION TECHNOLOGY, INC.", u) == "GD"
