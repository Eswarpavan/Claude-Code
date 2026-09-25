"""Paper-account settings (stored as JSON on the account; defaults below)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass
class PaperSettings:
    start_cash: float = 100.0
    risk_per_trade: float = 0.02          # fraction of equity lost if the stop is hit (before gaps)
    max_position_pct: float = 0.25        # of equity
    max_open_positions: int = 4
    cash_floor_pct: float = 0.10          # always keep this much cash
    min_order_usd: float = 1.0
    auto_buy: bool = False                # OFF by default (rule 6); also gated on calibration evidence
    auto_buy_threshold: float = 75.0
    auto_buy_min_closed_trades: int = 30
    time_stop_sessions: int = 10
    max_gap_up_pct: float = 5.0           # skip a buy if the open gaps up more than this over the signal price
    max_move_since_catalyst_pct: float = 15.0   # skip if the fill is this far above the pre-news close (EXTENDED)
    min_price: float = 3.0
    min_adv_usd: float = 2e6

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> PaperSettings:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
