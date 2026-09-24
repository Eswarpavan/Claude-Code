"""Backtest: no lookahead, next-open entries, honest gaps, purged walk-forward, baselines,
calibration verdicts, and the model staying OFF when it does not beat the baselines."""

import datetime as dt
import random

import pytest

from catalystedge.backtest import walkforward as wfm
from catalystedge.backtest.dataset import Bar, HistEvent, build_rows, simulate_trade
from catalystedge.core import calendar

UTC = dt.UTC


def walk(symbol_seed: int, n: int = 520, start=dt.date(2024, 7, 1), drift=0.0) -> list[Bar]:
    rnd = random.Random(symbol_seed)
    days = calendar.sessions_between(start, calendar.add_sessions(start, n))
    out, px = [], 50.0
    for d in days:
        o = px * (1 + rnd.gauss(0, 0.005))
        c = o * (1 + rnd.gauss(drift, 0.015))
        out.append(Bar(d, round(o, 4), round(max(o, c) * 1.005, 4), round(min(o, c) * 0.995, 4), round(c, 4),
                       rnd.randint(500_000, 2_000_000)))
        px = c
    return out


def events_for(symbols, bars, per_symbol=30, seed=1, kinds=("earnings_beat", "contract_win", "upgrade")):
    rnd = random.Random(seed)
    out = []
    for s in symbols:
        days = [b.date for b in bars[s][40:-15]]
        for d in rnd.sample(days, per_symbol):
            at = dt.datetime.combine(d, dt.time(12, 0), UTC)       # 08:00 ET: before the open
            out.append(HistEvent(s, rnd.choice(kinds), "filing", f"{s} event {d}", at,
                                 materiality=rnd.uniform(0.5, 0.95), credibility=1.0, ref=f"{s}{d}"))
    return out


@pytest.fixture(scope="module")
def data():
    syms = [f"S{i}" for i in range(12)]
    bars = {s: walk(i) for i, s in enumerate(syms)}
    spy = walk(99, drift=0.0003)
    rows = build_rows(events_for(syms, bars), bars, spy)
    return syms, bars, spy, rows


def test_features_never_see_the_future(data):
    syms, bars, spy, rows = data
    r = rows[40]
    s = r.event.symbol
    mutated = {k: list(v) for k, v in bars.items()}
    cut = next(i for i, b in enumerate(mutated[s]) if b.date == r.decision_date)
    mutated[s] = mutated[s][: cut + 1] + [Bar(b.date, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume * 9)
                                          for b in mutated[s][cut + 1:]]
    again = {(x.event.symbol, x.decision_date, x.catalyst): x for x in build_rows([r.event], mutated, spy)}
    twin = again[(s, r.decision_date, r.catalyst)]
    assert twin.features == r.features and twin.rule_score == r.rule_score


def test_entry_is_next_open_and_exit_never_same_day(data):
    _, bars, _, rows = data
    for r in rows:
        assert r.entry_date == calendar.next_session(r.decision_date)
        assert r.entry_price == next(b.open for b in bars[r.event.symbol] if b.date == r.entry_date)
        if r.trade_exit_date:
            assert r.trade_exit_date > r.entry_date and r.trade_sessions >= 1


def test_premarket_event_decides_same_day_after_close_event_next_day():
    bars = {"X": walk(5)}
    d = bars["X"][60].date
    pre = HistEvent("X", "earnings_beat", "filing", "pre", dt.datetime.combine(d, dt.time(12, 0), UTC))
    post = HistEvent("X", "contract_win", "filing", "post", dt.datetime.combine(d, dt.time(21, 30), UTC))
    rows = {r.catalyst: r for r in build_rows([pre, post], bars, walk(9))}
    assert rows["earnings_beat"].decision_date == d
    assert rows["contract_win"].decision_date == calendar.next_session(d)


def test_stop_gap_fills_at_next_open_below_the_stop():
    days = calendar.sessions_between(dt.date(2026, 1, 5), dt.date(2026, 1, 20))
    bars = [Bar(days[0], 100, 101, 99, 100, 1)]
    bars.append(Bar(days[1], 100, 100, 90, 91, 1))           # closes below the 95 stop
    bars.append(Bar(days[2], 80, 82, 79, 81, 1))              # gaps down the next morning
    bars += [Bar(d, 81, 82, 80, 81, 1) for d in days[3:]]
    ret, exit_day, reason, held = simulate_trade(bars, 0, stop=95, target=120, cost_pct=0.2)
    assert reason == "stop" and exit_day == days[2] and held == 2
    assert ret == pytest.approx((80 / 100 - 1) * 100 - 0.2)  # the gap is recorded, not hidden


def test_walk_forward_is_purged_and_embargoed(data):
    _, _, _, rows = data
    wf = wfm.run_walk_forward(rows, min_train=60)
    assert wf.probs, "some folds should train"
    usable = wf.rows
    for f in wf.folds:
        if not f.trained:
            continue
        cutoff = f.test_start - dt.timedelta(days=wfm.EMBARGO_DAYS)
        allowed = [r for r in usable if r.decision_date < cutoff and r.trade_exit_date and
                   r.trade_exit_date < f.test_start]
        assert f.n_train == len(allowed)
    for i in wf.probs:
        assert usable[i].decision_date >= wf.folds[0].test_start


def test_noise_model_is_not_enabled_and_report_is_complete(data):
    _, _, _, rows = data
    wf = wfm.run_walk_forward(rows, min_train=60)
    rep = wfm.build_report(wf, "synthetic")
    assert set(rep["strategies"]) == {"all_events", "rules", "model", "spy_same_days"}
    assert rep["verdict"]["model_enabled"] is False or wfm.beats(rep["strategies"]["model"],
                                                                 rep["strategies"]["spy_same_days"])
    assert "earnings_beat" in rep["by_catalyst"]["all_events"]
    assert [b["bucket"] for b in rep["confidence_buckets"]["rules"]] == ["0-50", "50-65.0", "65.0-80.0", "80.0-100"]
    assert "OFF" in rep["verdict"]["plain"] or "enabled" in rep["verdict"]["plain"]


def test_beats_requires_all_three_metrics():
    a = {"hit_rate": 0.6, "mean_pct": 1.0, "sharpe": 1.2}
    assert wfm.beats(a, {"hit_rate": 0.5, "mean_pct": 0.5, "sharpe": 0.5})
    assert not wfm.beats(a, {"hit_rate": 0.5, "mean_pct": 1.5, "sharpe": 0.5})
    assert not wfm.beats(a, {"hit_rate": None, "mean_pct": 0.5, "sharpe": 0.5})


def test_calibration_verdicts():
    e, buckets = wfm.ece([0.1] * 60 + [0.9] * 60, [False] * 54 + [True] * 6 + [True] * 54 + [False] * 6, bins=5)
    assert e <= 0.05 and wfm.verdict(e, buckets) == "good"
    e2, b2 = wfm.ece([0.9] * 60 + [0.1] * 60, [False] * 60 + [True] * 60, bins=5)
    assert wfm.verdict(e2, b2) == "poor"
    assert wfm.verdict(*wfm.ece([0.5] * 10, [True] * 10, bins=5)) == "insufficient"


def test_calibration_snapshot_has_mapping(data):
    _, _, _, rows = data
    wf = wfm.run_walk_forward(rows, min_train=60)
    snaps = wfm.calibration_snapshots(wf)
    assert snaps[0]["event_family"] == "all" and snaps[0]["mapping"]
    assert snaps[0]["verdict"] in ("good", "fair", "poor", "insufficient")


def test_timesfm_section_warns_when_not_helping(data):
    _, _, _, rows = data
    wf = wfm.run_walk_forward(rows, min_train=60)
    fc = {i: (1.0 if i % 2 else -1.0) for i in wf.probs}                   # uninformative forecasts
    rep = wfm.build_report(wf, "synthetic", fc, "overlaps pretraining")
    t = rep["timesfm"]
    assert t["leakage_warning"] == "overlaps pretraining" and "rules_with_timesfm_filter" in t
    assert isinstance(t["helps"], bool) and t["plain"]
