"""Optional Ollama explanations: off by default, text only, and harmless when Ollama is missing."""

import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from catalystedge import jobs
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core.kv import InMemoryKV
from catalystedge.ml import llm
from catalystedge.ml.llm import OllamaExplainer, annotate_signals, build_explainer, clean

NOW = dt.datetime(2026, 9, 24, 21, 0, tzinfo=dt.UTC)
GOOD = ("Acme won a large government contract. New contracts can lift revenue expectations. "
        "The deal may be smaller than it sounds.")


def ollama(chat_text=GOOD, models=("qwen3:4b",), chat_status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["stream"] is False and body["model"] == "qwen3:4b"
            return httpx.Response(chat_status, json={"message": {"role": "assistant", "content": chat_text}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused")
    return httpx.MockTransport(handler)


def fake_signal(**kw):
    base = dict(id=1, symbol="ACME", catalyst_type="contract_win", reason="Contract win from news.", risk_notes=[],
                confidence=72.0, displayed=True, status="active", features={"rule_components": {"base": 50}})
    return SimpleNamespace(**{**base, **kw})


def run(sigs, transport, kv=None, **kw):
    ex = OllamaExplainer("http://ollama:11434", "qwen3:4b", transport=transport)
    return annotate_signals(sigs, ex, kv or InMemoryKV(FrozenClock(NOW)), company_of=lambda s: "Acme Corp",
                            headlines_of=lambda s: ["Acme wins $2B Army contract"], **kw)


def test_off_by_default():
    settings = Settings(_env_file=None)
    assert settings.llm_enabled is False
    assert build_explainer(settings) is None
    assert annotate_signals([fake_signal()], None, InMemoryKV(), company_of=str, headlines_of=list) == {"status": "off"}


def test_adds_text_and_changes_nothing_else():
    sig = fake_signal()
    before = {k: getattr(sig, k) for k in ("confidence", "catalyst_type", "displayed", "status", "reason")}
    out = run([sig], ollama())
    assert out["status"] == "ready" and out["new"] == 1
    assert sig.features["llm_why"]["text"] == GOOD
    assert "does not affect the score" in sig.features["llm_why"]["label"]
    assert sig.features["rule_components"] == {"base": 50}
    assert {k: getattr(sig, k) for k in before} == before


@pytest.mark.parametrize("transport,status", [(unreachable(), "unreachable"),
                                              (ollama(models=("llama3.2:3b",)), "model_missing")])
def test_fails_safe_when_ollama_is_missing(transport, status):
    sig = fake_signal()
    out = run([sig], transport)
    assert out["status"] == status and "llm_why" not in sig.features


def test_bad_or_failing_answers_are_dropped():
    for transport in (ollama(chat_status=500), ollama(chat_text="You should buy now, this is guaranteed."),
                      ollama(chat_text="  ")):
        sig = fake_signal()
        out = run([sig], transport)
        assert out["failed"] == 1 and "llm_why" not in sig.features


def test_cached_answers_are_reused_and_new_calls_are_capped():
    calls, kv = [], InMemoryKV(FrozenClock(NOW))
    run([fake_signal()], ollama(calls=calls), kv)
    run([fake_signal()], ollama(calls=calls), kv)
    assert calls.count("/api/chat") == 1
    many = [fake_signal(id=i, symbol=f"S{i}") for i in range(5)]
    calls.clear()
    out = run(many, ollama(calls=calls), max_new=2)
    assert out["new"] == 2 and calls.count("/api/chat") == 2


def test_clean_strips_thinking_and_caps_length():
    assert clean("<think>secret plan</think> " + GOOD) == GOOD
    long = "This is a sentence about the catalyst. " * 40
    out = clean(long)
    assert len(out) <= llm.MAX_CHARS and out.endswith(".")


def test_unexpected_errors_never_escape():
    class Broken:
        model = "x"

        def status(self):
            raise RuntimeError("boom")

    out = annotate_signals([fake_signal()], Broken(), InMemoryKV(), company_of=str, headlines_of=list)
    assert out["status"].startswith("error")


# ----------------------------------------------------------------------------- with the database


@pytest.fixture
def clean_db(engine):
    yield engine
    with engine.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"))]
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.mark.db
@pytest.mark.parametrize("transport", [ollama(), unreachable()])
def test_job_writes_only_the_explanation(clean_db, monkeypatch, transport):
    from catalystedge.db.models import Signal, Ticker

    with sessionmaker(clean_db)() as s:
        s.add(Ticker(symbol="ACME", name="Acme Corp", aliases=[]))
        s.flush()
        sig = Signal(symbol="ACME", as_of_date=dt.date(2026, 9, 24), catalyst_type="contract_win", rule_id="R_X",
                     rule_score=72, confidence=72, expected_return_pct=3.0, expected_return_basis="prior",
                     holding_days_min=3, holding_days_max=10, entry_ref_price=Decimal("10"),
                     stop_price=Decimal("9.4"), target_price=Decimal("10.6"), suggested_size_usd=Decimal("20"),
                     risk_notes=[], reason="Contract win.", features={"engine": "engine-v1"}, displayed=True)
        s.add(sig)
        s.commit()
        sid = sig.id
    settings = Settings(_env_file=None, LLM_ENABLED="true", OLLAMA_URL="http://ollama:11434")
    monkeypatch.setattr(llm, "build_explainer",
                        lambda st: OllamaExplainer(st.ollama_url, st.ollama_model, transport=transport))
    clock = FrozenClock(NOW)
    ctx = jobs.Context(settings, sessionmaker(clean_db, expire_on_commit=False), None, InMemoryKV(clock), clock)
    out = jobs.job_llm_notes(ctx, [sid])
    with sessionmaker(clean_db)() as s:
        row = s.scalars(select(Signal).where(Signal.id == sid)).one()
        assert (row.confidence, row.catalyst_type, row.displayed, row.status) == (72, "contract_win", True, "active")
        assert row.features["engine"] == "engine-v1"
        if out["status"] == "ready":
            assert row.features["llm_why"]["text"] == GOOD
        else:
            assert out["status"] == "unreachable" and "llm_why" not in row.features
