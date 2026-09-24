"""TimesFM toggle and background job: persists, logs changes, default OFF, off = unchanged,
filter mode lists removed signals, failures fall back with a visible status."""

import datetime as dt
from pathlib import Path

import pytest

from catalystedge.ml import timeseries as tsm
from catalystedge.signals.engine import generate_signals
from catalystedge.signals.timesfm_hook import Forecast
from tests.test_signal_engine import AS_OF, NOW, add_bars, add_event

pytestmark = pytest.mark.db


class FakeService:
    def __init__(self, er=2.0, fail=False, status=("ready", "fake")):
        self.er, self.fail, self._status, self.auto_download, self.calls = er, fail, status, True, 0

    def status(self):
        return self._status

    def forecast(self, closes, as_of):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        er = self.er if not callable(self.er) else None
        return {s: Forecast(s, 10, er if er is not None else self.er(s), -1.0, 4.0, "fake", as_of.isoformat())
                for s in closes}


def test_default_is_off_and_toggle_persists_with_log(db):
    assert tsm.get_state(db).enabled is False
    tsm.set_state(db, True, "filter", by="test")
    s = tsm.get_state(db)
    assert (s.enabled, s.mode) == (True, "filter")
    tsm.set_state(db, True, "feature")
    tsm.set_state(db, False)
    log = tsm.changes(db)
    assert [(c["enabled"], c["mode"]) for c in log] == [(True, "filter"), (True, "feature"), (False, "feature")]
    assert log[0]["by"] == "test" and log[0]["was"] == {"enabled": False, "mode": "feature"}
    assert tsm.get_status(db)["status"] == "off"


def test_invalid_mode_keeps_previous(db):
    tsm.set_state(db, True, "filter")
    assert tsm.set_state(db, True, "nonsense").mode == "filter"


def test_job_is_a_noop_when_off(db):
    svc = FakeService()
    assert tsm.run_timesfm_job(db, Path("/nonexistent"), NOW, svc) == {"status": "off"} and svc.calls == 0


def test_job_caches_forecasts_and_engine_uses_them_immediately(db):
    add_bars(db, "TFA")
    add_event(db, "TFA")
    base = {c.symbol: c.confidence for c in generate_signals(db, NOW).candidates}["TFA"]
    tsm.set_state(db, True, "feature")
    out = tsm.run_timesfm_job(db, Path("/nonexistent"), NOW, FakeService(er=3.0))
    assert out["status"] == "ready" and out["forecasts"] == 1
    assert tsm.cached_forecasts(db, AS_OF)["TFA"].expected_return_pct == 3.0
    kw = tsm.engine_kwargs(db, NOW, Path("/nonexistent"))
    assert kw["timesfm_state"].enabled and kw["timesfm_status"] == "ready"
    c = {c.symbol: c for c in generate_signals(db, NOW, **kw).candidates}["TFA"]
    assert c.confidence > base and c.signal.features["timesfm"]["forecast"]["expected_return_pct"] == 3.0


def test_filter_mode_end_to_end_lists_removed(db):
    for s in ("UPX1", "DNX1"):
        add_bars(db, s)
        add_event(db, s)
    tsm.set_state(db, True, "filter")
    tsm.run_timesfm_job(db, Path("/nonexistent"), NOW, FakeService(er=lambda s: 2.0 if s == "UPX1" else -2.0))
    r = generate_signals(db, NOW, **tsm.engine_kwargs(db, NOW, Path("/nonexistent")))
    assert [c.symbol for c in r.filtered_by_timesfm] == ["DNX1"]
    assert {c.symbol for c in r.displayed} == {"UPX1"}


def test_failure_falls_back_with_visible_status(db):
    add_bars(db, "TFF")
    add_event(db, "TFF")
    base = {c.symbol: c for c in generate_signals(db, NOW).candidates}["TFF"]
    tsm.set_state(db, True, "filter")
    out = tsm.run_timesfm_job(db, Path("/nonexistent"), NOW, FakeService(fail=True))
    assert out["status"] == "failed" and tsm.get_status(db)["status"] == "failed"
    r = generate_signals(db, NOW, **tsm.engine_kwargs(db, NOW, Path("/nonexistent")))
    c = {c.symbol: c for c in r.candidates}["TFF"]
    assert c.displayed == base.displayed and c.confidence == base.confidence
    assert any("failed" in w for w in r.warnings)


def test_model_missing_status(db):
    tsm.set_state(db, True, "feature")
    svc = FakeService(status=("model missing", "weights not downloaded"))
    svc.auto_download = False
    assert tsm.run_timesfm_job(db, Path("/nonexistent"), NOW, svc)["status"] == "model missing"


def test_stale_cache_is_ignored(db):
    tsm._put(db, "timesfm_forecasts", {"as_of": (AS_OF - dt.timedelta(days=3)).isoformat(),
                                       "forecasts": {"X": Forecast("X", 10, 1, 0, 2, "m", "d").__dict__}})
    assert tsm.cached_forecasts(db, AS_OF) == {}


def test_summarize_uses_days_3_to_10():
    import numpy as np

    median = np.array([100, 100, 102, 102, 102, 102, 102, 102, 102, 102], dtype=float)
    q = np.stack([median - 2] + [median] * 7 + [median + 2], axis=1)
    f = tsm.summarize("X", 100.0, median, q, AS_OF)
    assert f.expected_return_pct == pytest.approx(2.0) and f.low_pct == pytest.approx(0.0)
    assert f.high_pct == pytest.approx(4.0)
