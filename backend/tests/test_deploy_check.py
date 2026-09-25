"""Deployment checks: scheduler heartbeat, test email through the worker path, cloud email schedule,
and `verify-deployment` run end to end against the real API."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from catalystedge import jobs
from catalystedge.api import main as api
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import Notification, RefreshRun
from catalystedge.deploy_check import run
from catalystedge.worker.celery_app import beat_schedule

NOW = dt.datetime(2026, 9, 24, 21, 0, tzinfo=dt.UTC)


def test_heartbeat_fresh_stale_and_missing():
    clock = FrozenClock(NOW)
    kv = InMemoryKV(clock)
    assert jobs.read_heartbeat(kv, NOW)["status"] == "no heartbeat yet"
    jobs.record_heartbeat(kv, "news", NOW)
    hb = jobs.read_heartbeat(kv, NOW + dt.timedelta(minutes=14))
    assert hb["status"] == "ok" and hb["last_task"] == "news" and hb["minutes_ago"] == 14
    assert jobs.read_heartbeat(kv, NOW + dt.timedelta(hours=2))["status"] == "stale"


def _minutes(entry) -> set[int]:
    return set(entry["schedule"].minute)


def test_cloud_email_schedule_lets_neon_sleep():
    local, cloud = beat_schedule("local"), beat_schedule("cloud")
    assert len(_minutes(local["email"])) == 60                      # local Postgres: every minute is fine
    # cloud: only on the news-poll minutes, when the database is awake anyway
    assert _minutes(cloud["email"]) == _minutes(cloud["news-market-hours"]) == {0, 15, 30, 45}
    assert _minutes(cloud["email-off-hours"]) == {0}


@pytest.fixture
def clean(engine):
    yield engine
    with engine.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"))]
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def deployed(engine, monkeypatch, email=True):
    """The API as deployed (password on, email configured), with the worker simulated in-process."""
    api._attempts.clear()
    kw = {"APP_PASSWORD": "correct horse"}
    if email:
        kw |= {"RESEND_API_KEY": "re_test_not_real", "ALERT_EMAIL_TO": "me@example.com"}
    settings = Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures", CATALYSTEDGE_PROFILE="cloud", **kw)
    app = api.create_app(settings, sessionmaker(engine, expire_on_commit=False))
    clock = FrozenClock(NOW)
    api.state.clock = clock
    kv = InMemoryKV(clock)
    ctx = jobs.Context(settings, api.state.session_factory, None, kv, clock)
    api.state.ctx = ctx
    provider = FakeProvider()
    monkeypatch.setattr(jobs, "build_provider", lambda st: provider if email else None)

    def worker_refresh(rid):                       # stands in for the Celery refresh task
        with api.state.session_factory() as s:
            s.get(RefreshRun, rid).status = "done"
            s.commit()

    def worker_email():
        jobs.job_email(ctx)
        jobs.record_heartbeat(kv, "email", clock.now())
        return "worker"

    monkeypatch.setattr(api, "_dispatch_refresh", worker_refresh)
    monkeypatch.setattr(api, "_dispatch_email", worker_email)
    return TestClient(app, base_url="https://ce.example"), kv, provider


def test_health_reports_scheduler_and_email(clean, monkeypatch):
    c, kv, _ = deployed(clean, monkeypatch)
    body = c.get("/health").json()
    assert body["scheduler"]["status"] == "no heartbeat yet" and body["email"] == "configured"
    jobs.record_heartbeat(kv, "news", NOW)
    assert c.get("/health").json()["scheduler"]["status"] == "ok"
    api.state.ctx = None


def test_test_email_is_sent_through_the_email_job(clean, monkeypatch):
    c, _, provider = deployed(clean, monkeypatch)
    from catalystedge.api.auth import issue_token

    h = {"Authorization": f"Bearer {issue_token(api.state.settings)}"}
    assert c.post("/api/notifications/test").status_code == 401
    r = c.post("/api/notifications/test", headers=h).json()
    assert r["sent_by"] == "worker" and provider.sent[0].subject == "CatalystEdge test email"
    with api.state.session_factory() as s:
        n = s.scalars(select(Notification)).one()
        assert (n.kind, n.status, n.id) == ("test", "sent", r["notification_id"])
    api.state.ctx = None


def test_test_email_refused_when_email_not_configured(clean, monkeypatch):
    c, _, _ = deployed(clean, monkeypatch, email=False)
    from catalystedge.api.auth import issue_token

    h = {"Authorization": f"Bearer {issue_token(api.state.settings)}"}
    r = c.post("/api/notifications/test", headers=h)
    assert r.status_code == 409 and "ALERT_EMAIL_TO" in r.json()["detail"]
    api.state.ctx = None


def test_verify_deployment_end_to_end(clean, monkeypatch):
    c, kv, provider = deployed(clean, monkeypatch)
    jobs.record_heartbeat(kv, "news", NOW - dt.timedelta(minutes=5))
    lines = []
    code = run("https://ce.example", email=True, password="correct horse", client=c, sleep=lambda s: None,
               out=lines.append)
    report = "\n".join(lines)
    assert code == 0, report
    for check in ("API reachable", "Database", "Scheduler running", "Login", "Password required", "Refresh",
                  "Test email sent"):
        assert f"[PASS] {check}" in report
    assert "correct horse" not in report and "Bearer" not in report       # never prints the password or token
    assert len(provider.sent) == 1
    api.state.ctx = None


def test_verify_deployment_reports_wrong_password_and_dead_scheduler(clean, monkeypatch):
    c, kv, _ = deployed(clean, monkeypatch)
    jobs.record_heartbeat(kv, "news", NOW - dt.timedelta(hours=3))
    lines = []
    code = run("https://ce.example", password="wrong", client=c, sleep=lambda s: None, out=lines.append)
    report = "\n".join(lines)
    assert code == 1
    assert "[FAIL] Scheduler running: stale" in report and "[FAIL] Login" in report
    assert "wrong" not in report.replace("[FAIL]", "")
    api.state.ctx = None


@pytest.mark.parametrize("given,expected", [
    ("postgresql://u:p@ep-x-pooler.neon.tech/db?sslmode=require",
     "postgresql+psycopg://u:p@ep-x-pooler.neon.tech/db?sslmode=require"),
    ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
])
def test_neon_connection_strings_work_as_pasted(given, expected):
    from catalystedge.db.session import normalize_url

    assert normalize_url(given) == expected


@pytest.mark.db
def test_fresh_database_starts_from_the_real_backtest_verdicts(clean):
    """A new computer or a new Neon database must not re-enable catalysts the backtest switched off."""
    from catalystedge.signals.catalyst_status import catalyst_status

    with sessionmaker(clean)() as s:
        st = catalyst_status(s)
    assert st["earnings_beat"]["status"] == "disabled" and st["fda_approval"]["status"] == "disabled"
    assert st["insider_buy_cluster"]["status"] == "enabled"
    assert st["contract_win"]["status"] == "untested"
    assert all(v["basis"] == "baseline backtest" for v in st.values())
