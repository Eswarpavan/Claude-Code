"""Email alerts: dedupe, thresholds, rate limits, retries with backoff, send log, content."""

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from catalystedge.config import Settings
from catalystedge.db.models import Event, Notification, SignalEvent
from catalystedge.notify.dispatcher import (
    FOOTER,
    MAX_ATTEMPTS,
    dispatch,
    queue_high_confidence,
    queue_paper_buy,
    render,
)
from catalystedge.notify.providers import ResendProvider, build_provider
from catalystedge.paper.engine import execute_orders, get_account, manual_buy
from tests.test_paper import FRI, LIQUID, make_signal, opens

pytestmark = pytest.mark.db
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 25, 14, 0, tzinfo=UTC)


class FakeProvider:
    name = "fake"

    def __init__(self, fail_times=0):
        self.sent, self.fail_times = [], fail_times

    def send(self, msg):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("smtp down")
        self.sent.append(msg)


def attach_event(db, sig, headline="ABC wins contract"):
    ev = Event(symbol=sig.symbol, event_type="contract_win", origin="news", headline=headline, url="u",
               available_at=NOW, polarity="positive", classifier_version="t")
    db.add(ev)
    db.flush()
    db.add(SignalEvent(signal_id=sig.id, event_id=ev.id))
    db.flush()
    return ev


def test_high_confidence_threshold_and_display_rule(db):
    assert not queue_high_confidence(db, make_signal(db, "LOW", confidence=79.9))
    assert not queue_high_confidence(db, make_signal(db, "HID", confidence=90, displayed=False))
    sig = make_signal(db, "ABC", confidence=85)
    attach_event(db, sig)
    assert queue_high_confidence(db, sig)


def test_one_email_per_ticker_per_event(db):
    sig = make_signal(db, "ABC", confidence=85)
    ev = attach_event(db, sig)
    assert queue_high_confidence(db, sig)
    # Next day the same event produces another signal row: no second email.
    sig2 = make_signal(db, "ABC", confidence=88, as_of=FRI)
    db.add(SignalEvent(signal_id=sig2.id, event_id=ev.id))
    db.flush()
    assert not queue_high_confidence(db, sig2)
    assert len(db.scalars(select(Notification)).all()) == 1


def test_paper_buy_email_after_fill(db):
    acct = get_account(db)
    sig = make_signal(db, "ABC", confidence=70)
    order = manual_buy(db, acct, sig, dt.datetime(2026, 9, 24, 21, tzinfo=UTC))
    assert not queue_paper_buy(db, order)                      # not filled yet
    execute_orders(db, acct, FRI, opens({("ABC", FRI): 100.0}), LIQUID)
    assert queue_paper_buy(db, order) and not queue_paper_buy(db, order)
    n = db.scalars(select(Notification)).one()
    assert n.kind == "paper_buy" and any("UNCALIBRATED" in line for line in n.payload["lines"])
    assert any("shares" in line for line in n.payload["lines"])


def test_dispatch_sends_and_logs(db):
    sig = make_signal(db, "ABC", confidence=85)
    attach_event(db, sig)
    queue_high_confidence(db, sig)
    p = FakeProvider()
    r = dispatch(db, p, "me@example.com", NOW)
    assert r.sent == 1 and p.sent[0].subject.startswith("CatalystEdge: ABC signal 85% (UNCALIBRATED)")
    n = db.scalars(select(Notification)).one()
    assert n.status == "sent" and n.sent_at == NOW and n.provider == "fake"
    assert dispatch(db, p, "me@example.com", NOW).sent == 0      # never resent


def test_retry_with_backoff_then_success(db):
    sig = make_signal(db, "ABC", confidence=85)
    attach_event(db, sig)
    queue_high_confidence(db, sig)
    p = FakeProvider(fail_times=1)
    assert dispatch(db, p, "me@example.com", NOW).failed == 1
    n = db.scalars(select(Notification)).one()
    assert n.status == "failed" and "smtp down" in n.last_error
    assert dispatch(db, p, "me@example.com", NOW + dt.timedelta(seconds=30)).deferred == 1   # backoff: 1 minute
    assert dispatch(db, p, "me@example.com", NOW + dt.timedelta(minutes=2)).sent == 1


def test_gives_up_after_max_attempts(db):
    sig = make_signal(db, "ABC", confidence=85)
    attach_event(db, sig)
    queue_high_confidence(db, sig)
    p = FakeProvider(fail_times=99)
    t = NOW
    for _ in range(MAX_ATTEMPTS + 2):
        dispatch(db, p, "me@example.com", t)
        t += dt.timedelta(hours=3)
    n = db.scalars(select(Notification)).one()
    assert n.attempts == MAX_ATTEMPTS and n.status == "failed"


def test_daily_cap_and_per_run_limit(db):
    for i in range(5):
        sig = make_signal(db, f"S{i}", confidence=85)
        attach_event(db, sig, f"S{i} wins contract")
        queue_high_confidence(db, sig)
    p = FakeProvider()
    r = dispatch(db, p, "me@example.com", NOW, daily_cap=3, per_run=2)
    assert r.sent == 2 and r.deferred == 3
    r = dispatch(db, p, "me@example.com", NOW, daily_cap=3, per_run=2)
    assert r.sent == 1 and "rate limit" in r.skipped_reason


def test_without_provider_messages_stay_queued(db):
    sig = make_signal(db, "ABC", confidence=85)
    attach_event(db, sig)
    queue_high_confidence(db, sig)
    r = dispatch(db, None, None, NOW)
    assert r.sent == 0 and "no email provider" in r.skipped_reason
    assert db.scalars(select(Notification)).one().status == "queued"


def test_render_has_footer_and_escapes_html():
    e = render({"subject": "s", "lines": ["<b>x</b>"]}, "me@example.com")
    assert FOOTER in e.text and "&lt;b&gt;" in e.html


def test_provider_selection_and_key_not_leaked():
    assert build_provider(Settings(_env_file=None)) is None
    assert build_provider(Settings(_env_file=None, RESEND_API_KEY="re_SECRET")).name == "resend"

    def denied(req):
        return httpx.Response(401, text=f"bad key {req.headers['authorization']}")

    p = ResendProvider("re_SECRET", "a@b.c", transport=httpx.MockTransport(denied))
    with pytest.raises(RuntimeError) as e:
        p.send(render({"subject": "s", "lines": []}, "me@example.com"))
    assert "re_SECRET" not in str(e.value)
