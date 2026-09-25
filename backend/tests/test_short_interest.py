"""FINRA short interest: parsed and stored as context; honest 'auth required' when FINRA refuses."""

import datetime as dt
import json

import httpx
from sqlalchemy import select

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import ShortInterest
from catalystedge.events.short_interest import ingest_short_interest, short_interest_note

NOW = dt.datetime(2026, 9, 24, 22, 0, tzinfo=dt.UTC)
ROWS = [{"symbolCode": "ACME", "settlementDate": "2026-09-15", "currentShortPositionQuantity": 1250000,
         "averageDailyVolumeQuantity": 250000, "daysToCoverQuantity": 5.0}]


def client(handler):
    clock = FrozenClock(NOW)
    return HttpClient(transport=httpx.MockTransport(handler), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)


def test_stores_latest_settlement_and_makes_a_note(db):
    bodies = []

    def h(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=ROWS)

    rep = ingest_short_interest(db, client(h), ["ACME"])
    assert rep.stored == 1 and bodies[0]["compareFilters"][0]["fieldValue"] == "ACME"
    assert db.scalar(select(ShortInterest)).days_to_cover == 5.0
    note = short_interest_note(db, "ACME")
    assert "1,250,000 shares, 5.0 days to cover" in note["text"] and note["settlement_date"] == "2026-09-15"


def test_refusal_is_reported_as_auth_required_and_secrets_stay_hidden(db):
    rep = ingest_short_interest(db, client(lambda r: httpx.Response(401, text="unauthorized")), ["ACME"])
    assert rep.status == "disabled" and "auth required" in rep.errors[0]

    def token_fails(req):
        return httpx.Response(401, text=f"bad credentials {req.headers.get('authorization')}")

    rep = ingest_short_interest(db, client(token_fails), ["ACME"], "CLIENTID", "SECRETXYZ")
    assert rep.status == "failed" and "SECRETXYZ" not in " ".join(rep.errors)
    assert "Q0xJRU5USUQ6U0VDUkVUWFla" not in " ".join(rep.errors)       # base64 of the credentials
