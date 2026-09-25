"""Slicing with a significance bar and a multiple-comparisons correction: noise must not pass."""

import datetime as dt
import random
from types import SimpleNamespace

import pytest

from catalystedge.backtest import slices


@pytest.mark.parametrize("sic,sector", [(3571, "Technology"), (7370, "Technology"), (6021, "Financials"),
                                        (2911, "Energy"), (2834, "Health care"), (3711, "Consumer"),
                                        (5331, "Consumer"), (3531, "Industrials"), (4215, "Industrials"),
                                        (None, "Unknown")])
def test_sector_from_sec_industry_code(sic, sector):
    assert slices.sector_of(sic) == sector


def test_shares_are_point_in_time():
    meta = {"shares": [["2025-01-30", "2025-01-20", 100.0], ["2025-05-01", "2025-04-20", 120.0]]}
    assert slices.shares_at(meta, dt.date(2025, 3, 1)) == 100.0          # the May filing is not known yet
    assert slices.shares_at(meta, dt.date(2025, 6, 1)) == 120.0
    assert slices.shares_at(meta, dt.date(2024, 12, 1)) is None
    assert slices.cap_bucket(15e9).startswith("Large") and slices.cap_bucket(1e8).startswith("Micro")


def test_sign_flip_p_value():
    rnd = random.Random(1)
    assert slices.sign_flip_p([2 + rnd.gauss(0, 1) for _ in range(60)], n_perm=2000) < 0.01
    assert slices.sign_flip_p([rnd.gauss(0, 1) for _ in range(60)], n_perm=2000) > 0.05


def _row(sym, edge, rnd, day):
    spy = rnd.gauss(0.2, 2)
    ret = spy + edge + rnd.gauss(0, 4)
    return SimpleNamespace(event=SimpleNamespace(symbol=sym), rule_score=70.0, skip_reason=None, catalyst="upgrade",
                           trade_return=ret, spy_return=spy, trade_sessions=5, decision_date=day,
                           labels={h: ret for h in (1, 3, 5, 10)}, spy_labels={h: spy for h in (1, 3, 5, 10)})


def _data(edge_for_tech: float, n_per_sector: int = 80, seed: int = 5):
    rnd = random.Random(seed)
    meta = {"TEC": {"sic": 3571, "shares": [["2024-01-01", "2023-12-31", 1e9]]},
            "BNK": {"sic": 6021, "shares": [["2024-01-01", "2023-12-31", 1e7]]},
            "OIL": {"sic": 2911, "shares": [["2024-01-01", "2023-12-31", 1e8]]}}
    rows, closes = [], {}
    for k in range(n_per_sector):
        day = dt.date(2025, 10, 1) + dt.timedelta(days=k)
        for sym in meta:
            rows.append(_row(sym, edge_for_tech if sym == "TEC" else 0.0, rnd, day))
            closes[(sym, day)] = 50.0
    return rows, meta, closes


def test_pure_noise_produces_no_candidate_and_says_so():
    out = slices.analyse(*_data(edge_for_tech=0.0))
    assert out["candidates"] == [] and "No slice passes" in out["plain"]
    m = out["comparisons"]
    assert m == len(out["slices"]) and out["alpha_per_test"] == pytest.approx(0.05 / m)
    assert all(t["min_detectable_excess_pct"] > 0 for t in out["slices"] if t["n"] >= 50)


def test_a_large_real_edge_is_found_and_small_slices_never_qualify():
    out = slices.analyse(*_data(edge_for_tech=4.0))
    assert "Technology" in out["candidates"]
    assert "Financials" not in out["candidates"] and "Energy" not in out["candidates"]
    few = slices.analyse(*_data(edge_for_tech=4.0, n_per_sector=30))      # same edge, only 30 trades
    assert few["candidates"] == []


def test_many_slices_raise_the_bar():
    """The correction makes each test stricter: a raw p of 0.01 fails once there are 10+ slices."""
    out = slices.analyse(*_data(edge_for_tech=0.0))
    m = out["comparisons"]
    assert m >= 10 and all(t["p_bonferroni"] == pytest.approx(min(1.0, t["p_value"] * m))
                           for t in out["slices"] if t["p_value"] is not None)


def test_closest_catalyst_and_trades_needed_are_reported():
    out = slices.analyse(*_data(edge_for_tech=0.0))
    cl = out["closest_catalyst"]
    assert cl["catalyst"] == "upgrade" and cl["passes"] is False and cl["plain"].startswith("Closest catalyst")
    assert slices.trades_needed(0.5, 4.0, 0.05 / 12) > slices.trades_needed(1.0, 4.0, 0.05 / 12) > 0
    assert slices.trades_needed(-0.2, 4.0, 0.01) is None

