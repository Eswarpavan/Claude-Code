"""Trading-cost assumptions: half the bid-ask spread plus slippage, by liquidity.

Defaults are deliberately conservative for a small retail account. There are
no commissions (US retail brokers charge none), but a fee field exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    # (min 20-day average dollar volume, half-spread bps, slippage bps), most liquid first.
    buckets: tuple[tuple[float, float, float], ...] = (
        (50e6, 5.0, 5.0),
        (10e6, 15.0, 10.0),
        (2e6, 30.0, 20.0),
        (0.0, 60.0, 40.0),    # below the liquidity floor; only reachable for manual buys
    )
    fee_per_order: float = 0.0

    def bps(self, adv_usd: float | None) -> tuple[float, float]:
        adv = adv_usd if adv_usd is not None else 0.0
        for floor, spread, slip in self.buckets:
            if adv >= floor:
                return spread, slip
        return self.buckets[-1][1:]

    def fill_price(self, raw_open: float, side: str, adv_usd: float | None) -> tuple[float, float, float]:
        """(fill price, half-spread bps, slippage bps). Buys pay up, sells receive less."""
        spread, slip = self.bps(adv_usd)
        mult = 1 + (spread + slip) / 1e4 if side == "buy" else 1 - (spread + slip) / 1e4
        return raw_open * mult, spread, slip

    def round_trip_pct(self, adv_usd: float | None) -> float:
        spread, slip = self.bps(adv_usd)
        return 2 * (spread + slip) / 1e4 * 100
