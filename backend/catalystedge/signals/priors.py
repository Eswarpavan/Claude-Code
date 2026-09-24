"""Per-catalyst priors (Phase 1, labelled `basis: prior` and UNCALIBRATED).

`weight` sets how much a catalyst type can contribute to the rule score. Analyst
upgrades are deliberately LOW weight: they are frequent, often already priced in,
and in the first live run they were most of the "signals". Expected returns and
holding periods are conservative literature-style priors, not measurements; the
backtest and live outcomes replace them once enough evidence exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalystPrior:
    catalyst: str
    weight: float                 # 0..1, scales the base score
    expected_return_pct: float    # median next-open to exit, before costs
    holding_days: tuple[int, int]
    rule_id: str
    plain: str                    # plain-English description for "why"


PRIORS: dict[str, CatalystPrior] = {p.catalyst: p for p in [
    CatalystPrior("fda_approval", 0.90, 3.0, (3, 10), "R-FDA-v1", "FDA approval"),
    CatalystPrior("positive_trial", 0.85, 4.0, (3, 10), "R-TRIAL-v1", "positive clinical-trial result"),
    CatalystPrior("guidance_raise", 0.85, 3.0, (3, 10), "R-GUIDE-v1", "raised guidance"),
    CatalystPrior("earnings_beat", 0.75, 2.0, (3, 10), "R-EARN-v1", "earnings beat (post-earnings drift)"),
    CatalystPrior("insider_buy_cluster", 0.75, 2.5, (5, 10), "R-INSIDER-v1", "cluster of insider open-market buys"),
    CatalystPrior("contract_win", 0.70, 2.0, (3, 10), "R-CONTRACT-v1", "material contract win"),
    CatalystPrior("m_and_a_target", 0.55, 1.0, (1, 5), "R-MNA-v1",
                  "announced acquisition target (upside usually capped by the deal price)"),
    CatalystPrior("upgrade", 0.20, 1.0, (3, 5), "R-UPGRADE-v1", "analyst upgrade or price-target raise"),
]}

CATALYSTS = tuple(PRIORS)
