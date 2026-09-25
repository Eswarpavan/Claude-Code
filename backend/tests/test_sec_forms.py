"""More SEC forms: spaced form names (EDGAR's "SCHEDULE 13D" rename), tender offers, contradiction flags."""

import datetime as dt
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from catalystedge.adapters.events import sec
from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Event, Filing
from catalystedge.events.sec_forms import classify_form, filing_flags, ingest_sec_forms, risk_notes
from tests.test_event_sources import NOW, UA, U, feed

T = "2026-09-23T10:00:00-04:00"


def test_parser_accepts_spaced_form_names_and_two_word_roles():
    xml = feed(("SCHEDULE 13D/A", "Wood River Capital, LLC", "0001888978", "Filed by", "0001888978-26-000001", "", T),
               ("SCHEDULE 13D/A", "Acme Robotics Inc.", "0001111111", "Subject", "0001888978-26-000001", "", T),
               ("NT 10-K", "Widget Holdings Corp", "0003333333", "Filer", "0003333333-26-000002", "", T),
               ("SC TO-T", "Bioxcel Therapeutics, Inc.", "0002222222", "Subject", "0009999999-26-000003", "", T))
    got = [(e.form, e.role, e.cik) for e in sec.parse_feed(xml)]
    assert got == [("SCHEDULE 13D/A", "Filed by", "0001888978"), ("SCHEDULE 13D/A", "Subject", "0001111111"),
                   ("NT 10-K", "Filer", "0003333333"), ("SC TO-T", "Subject", "0002222222")]


@pytest.mark.parametrize("form,kind", [("424B5", "dilution"), ("S-3ASR", "dilution"), ("NT 10-Q", "late_filing"),
                                       ("SCHEDULE 13D", "activist_stake"), ("SC 13G/A", "passive_stake"),
                                       ("SC TO-T", "tender_offer"), ("SC TO-I", "issuer_tender"),
                                       ("10-K", "periodic_report"), ("8-K", None)])
def test_form_kinds(form, kind):
    assert (classify_form(form).kind if classify_form(form) else None) == kind


def _client():
    feeds = {
        "SCHEDULE 13": feed(("SCHEDULE 13D", "Big Fund LP", "0008888888", "Filed by", "0008888888-26-000010", "", T),
                            ("SCHEDULE 13D", "Acme Robotics Inc.", "0001111111", "Subject", "0008888888-26-000010",
                             "", T)),
        "424B": feed(("424B5", "Acme Robotics Inc.", "0001111111", "Filer", "0001111111-26-000011", "", T)),
        "NT 10": feed(("NT 10-Q", "Widget Holdings Corp", "0003333333", "Filer", "0003333333-26-000012", "", T)),
        "SC TO": feed(("SC TO-T", "Bioxcel Therapeutics, Inc.", "0002222222", "Subject", "0007777777-26-000013", "",
                       T)),
    }

    def handler(req):
        typ = parse_qs(urlsplit(str(req.url)).query).get("type", [""])[0]
        return httpx.Response(200, text=feeds.get(typ, feed()))

    clock = FrozenClock(NOW)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def test_ingest_stores_forms_and_turns_tender_offers_into_m_and_a(db):
    rep = ingest_sec_forms(db, _client(), UA, U, NOW)
    forms = {f.symbol: f.form_type for f in db.scalars(select(Filing))}
    assert forms == {"ACME": "424B5", "WIDG": "NT 10-Q", "BIOX": "SC TO-T"} | {"ACME": forms["ACME"]}
    assert db.scalar(select(Filing).where(Filing.accession == "0008888888-26-000010")).symbol == "ACME"  # subject
    [ev] = db.scalars(select(Event)).all()
    assert (ev.symbol, ev.event_type, ev.polarity, ev.verification) == ("BIOX", "m_and_a_target", "positive",
                                                                         "primary")
    assert rep.stored == 4 and rep.events == 1
    assert ingest_sec_forms(db, _client(), UA, U, NOW).stored == 0            # idempotent


def test_contradiction_flags_are_notes_not_score_changes(db):
    ingest_sec_forms(db, _client(), UA, U, NOW)
    flags = filing_flags(db, "ACME", NOW)
    assert {f["kind"] for f in flags} == {"dilution", "activist_stake"}
    notes = risk_notes(flags)
    assert len(notes) == 1 and "dilution risk" in notes[0] and "Not yet part of the score" in notes[0]
    assert filing_flags(db, "ACME", NOW + dt.timedelta(days=40)) == []        # only recent filings
    assert "Late-filing" in risk_notes(filing_flags(db, "WIDG", NOW))[0]
