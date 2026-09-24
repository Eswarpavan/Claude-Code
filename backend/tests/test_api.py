"""API: auth, positive-only signals, filters, portfolio, buy/sell rules, calibration, refresh + SSE."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from catalystedge.api import main as api
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.db.models import Event, PaperOrder, Signal, SignalEvent, SignalOutcome, Ticker

pytestmark = pytest.mark.db
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 24, 22, 0, tzinfo=UTC)       # Thursday evening ET


@pytest.fixture
def clean(engine):
    yield engine
    with engine.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"))]
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


def client(engine, **kw):
    settings = Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures", **kw)
    app = api.create_app(settings, sessionmaker(engine, expire_on_commit=False))
    api.state.clock = FrozenClock(NOW)
    return TestClient(app)


def seed(engine, symbol="ABC", conf=85.0, displayed=True, sector="Technology", cap=5e9, catalyst="contract_win"):
    with sessionmaker(engine)() as s:
        if s.get(Ticker, symbol) is None:
            s.add(Ticker(symbol=symbol, name=f"{symbol} Inc", sector=sector, market_cap=Decimal(str(cap)),
                         aliases=[], adv20_usd=Decimal("1e9")))
            s.flush()
        sig = Signal(symbol=symbol, as_of_date=dt.date(2026, 9, 24), catalyst_type=catalyst, rule_id="R_X",
                     rule_score=conf, confidence=conf, expected_return_pct=3.5, expected_return_basis="prior",
                     holding_days_min=3, holding_days_max=10, entry_ref_price=Decimal("100"),
                     stop_price=Decimal("94"), target_price=Decimal("106"), suggested_size_usd=Decimal("20"),
                     risk_notes=["small cap"], reason="because", features={"model_disagrees": False},
                     displayed=displayed)
        ev = Event(symbol=symbol, event_type=catalyst, origin="news", headline=f"{symbol} wins contract",
                   url="https://example.com/x", source_key="finnhub_news", available_at=NOW - dt.timedelta(hours=3),
                   polarity="positive", strength="normal", sentiment={"model": "finbert", "pos": 0.8, "neu": 0.15,
                                                                        "neg": 0.05}, classifier_version="t")
        s.add_all([sig, ev])
        s.flush()
        s.add(SignalEvent(signal_id=sig.id, event_id=ev.id))
        s.commit()
        return sig.id


def test_health_is_public(clean):
    r = client(clean, APP_PASSWORD="pw").get("/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"


def test_password_login_and_token(clean):
    c = client(clean, APP_PASSWORD="correct horse", APP_SECRET="s3cret")
    assert c.get("/api/signals").status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    token = c.post("/api/login", json={"password": "correct horse"}).json()["token"]
    assert c.get("/api/signals", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert c.get(f"/api/signals?token={token}").status_code == 401        # never via URL


def test_login_rate_limited(clean):
    c = client(clean, APP_PASSWORD="pw")
    codes = [c.post("/api/login", json={"password": "x"}).status_code for _ in range(7)]
    assert codes[-1] == 429


def test_cloud_profile_requires_password(clean):
    c = client(clean, CATALYSTEDGE_PROFILE="cloud")
    assert c.get("/api/signals").status_code == 503


def test_signals_positive_only_threshold_and_labels(clean):
    seed(clean, "ABC", 85)
    seed(clean, "DEF", 70)
    seed(clean, "HID", 90, displayed=False)
    c = client(clean)
    body = c.get("/api/signals").json()
    syms = [x["symbol"] for x in body["signals"]]
    assert syms == ["ABC", "DEF"]                                     # ranked; hidden never shown
    top = body["signals"][0]
    assert top["highlight"] is True and top["confidence_label"] == "UNCALIBRATED"
    assert top["headlines"][0]["url"].startswith("https://") and top["hours_since_news"] == 3.0
    assert top["sentiment"]["pos"] == 0.8 and top["stop_price"] == 94.0
    assert [x["symbol"] for x in c.get("/api/signals?min_confidence=80").json()["signals"]] == ["ABC"]
    assert c.get("/api/signals?min_confidence=10").json()["signals"][-1]["confidence"] >= 65   # floor stays 65


def test_signal_filters(clean):
    seed(clean, "ABC", 85, sector="Technology", cap=5e9)
    seed(clean, "BIO", 75, sector="Health Care", cap=3e8, catalyst="fda_approval")
    c = client(clean)
    assert [x["symbol"] for x in c.get("/api/signals?sector=Health Care").json()["signals"]] == ["BIO"]
    assert [x["symbol"] for x in c.get("/api/signals?cap_min=1000000000").json()["signals"]] == ["ABC"]
    assert [x["symbol"] for x in c.get("/api/signals?catalyst=fda_approval").json()["signals"]] == ["BIO"]


def test_news_feed_positive_only(clean):
    seed(clean, "ABC")
    with sessionmaker(clean)() as s:
        s.add(Event(symbol="ABC", event_type="other", origin="news", headline="ABC cuts guidance", url="u",
                    available_at=NOW - dt.timedelta(hours=1), polarity="negative", classifier_version="t"))
        s.commit()
    items = client(clean).get("/api/news").json()["items"]
    assert [i["headline"] for i in items] == ["ABC wins contract"]


def test_manual_buy_next_open_and_no_same_day_sell(clean):
    sid = seed(clean, "ABC", 85)
    c = client(clean)
    r = c.post("/api/portfolio/buy", json={"signal_id": sid}).json()
    assert r["execute_on"] == "2026-09-25"
    assert c.post("/api/portfolio/buy", json={"signal_id": sid}).status_code in (200, 409)   # idempotent
    port = c.get("/api/portfolio").json()
    assert port["pending_orders"][0]["execute_on"] == "2026-09-25" and port["auto_buy"]["allowed"] is False
    assert "next session" in port["rules"]
    with sessionmaker(clean)() as s:
        assert len(s.scalars(select(PaperOrder)).all()) == 1


def test_sell_of_unknown_position_rejected(clean):
    assert client(clean).post("/api/portfolio/sell", json={"position_id": 999}).status_code == 409


def test_settings_cannot_force_auto_buy(clean):
    c = client(clean)
    r = c.put("/api/settings", json={"auto_buy": True}).json()
    assert r["paper"]["auto_buy"] is True and r["auto_buy_effective"] is False and "locked" in r["auto_buy_gate"]
    assert c.put("/api/settings", json={"max_position_pct": 0.9}).status_code == 422     # capped at 25%


def test_calibration_page_says_uncalibrated_and_buckets(clean):
    sid = seed(clean, "ABC", 82)
    with sessionmaker(clean)() as s:
        s.add(SignalOutcome(signal_id=sid, horizon_days=10, entry_date=dt.date(2026, 9, 25), entry_price=100,
                            exit_date=dt.date(2026, 10, 8), exit_price=104, return_pct=3.8, hit=True))
        s.commit()
    body = client(clean).get("/api/calibration").json()
    assert body["status"]["label"] == "UNCALIBRATED" and "30 closed paper trades" in body["status"]["message"]
    assert not body["status"]["message"].startswith("UNCALIBRATED")          # the UI adds the label once
    b = {x["bucket"]: x for x in body["live_buckets"]["10"]}
    assert b["80-85"]["n"] == 1 and b["80-85"]["hit_rate"] == 100.0 and b["65-70"]["n"] == 0


def test_sources_page(clean):
    from catalystedge.pipeline.store import seed_sources

    with sessionmaker(clean)() as s:
        seed_sources(s)
        s.commit()
    body = client(clean).get("/api/sources").json()
    health = {x["key"]: x["health"] for x in body["sources"]}
    assert health["benzinga_news"] == "disabled" and health["finnhub_news"] == "stale"
    assert body["email"]["provider"] is None and body["models"]


def test_refresh_start_cooldown_and_sse(clean, monkeypatch):
    from catalystedge import jobs
    from catalystedge.core.http import HttpClient
    from catalystedge.core.kv import InMemoryKV
    from catalystedge.fixtures import FixtureTransport
    from catalystedge.ml.sentiment import LexiconSentiment

    c = client(clean)
    clock = FrozenClock(NOW)
    kv = InMemoryKV(clock)
    ctx = jobs.Context(api.state.settings, api.state.session_factory,
                       HttpClient(transport=FixtureTransport(), kv=kv, clock=clock, sleep=lambda s: None), kv, clock)
    ctx._model = LexiconSentiment()
    api.state.ctx = ctx
    monkeypatch.setattr(api, "_dispatch_refresh", lambda rid: jobs.run_refresh(ctx, rid))   # synchronous
    first = c.post("/api/refresh").json()
    assert first["started"] is True
    again = c.post("/api/refresh").json()
    assert again["started"] is False and again["refresh_id"] == first["refresh_id"]
    events = []
    with c.stream("GET", f"/api/refresh/{first['refresh_id']}/events") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert events[-1]["refresh"]["status"] == "done" and events[-1]["refresh"]["progress_pct"] == 100.0
    assert {x["key"] for x in events[-1]["sources"]} >= {"finnhub_news", "benzinga_news"}
    api.state.ctx = None
