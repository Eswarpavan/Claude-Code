"""Stage-1 rule score (0-100), transparent and additive. Phase 1: UNCALIBRATED.

Every component's contribution is returned, stored on the signal and shown in the
UI ("which rule triggered and why"). Rule 1 (news first): a score is only ever
computed for an existing positive event; features adjust it, never create it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from catalystedge.signals.features import PriceFeatures
from catalystedge.signals.priors import CatalystPrior

# Priced-in rule (ARCHITECTURE §9.1.8): skip when the stock already moved hard since the news.
PRICED_IN_ATR = 1.5
PRICED_IN_MIN_PCT = 3.0
PRICED_IN_ABS_PCT = 10.0
MIN_ADV_USD = 2e6


class EventLike(Protocol):
    event_type: str
    materiality: float | None
    novelty: float | None
    credibility: float | None
    strength: str | None
    sentiment: dict | None
    source_key: str | None
    origin: str


@dataclass
class RuleScore:
    score: float
    components: dict[str, float]
    skip_reason: str | None = None
    risk_notes: list[str] = field(default_factory=list)
    model_disagrees: bool = False


def _model_disagrees(e: EventLike) -> bool:
    return bool(e.sentiment and e.sentiment.get("model_disagrees"))


def priced_in(f: PriceFeatures | None) -> bool:
    if f is None or f.reaction_pct is None:
        return False
    if f.reaction_pct >= PRICED_IN_ABS_PCT:
        return True
    return f.reaction_atr is not None and f.reaction_atr > PRICED_IN_ATR and f.reaction_pct >= PRICED_IN_MIN_PCT


def score(prior: CatalystPrior, events: Sequence[EventLike], f: PriceFeatures | None) -> RuleScore:
    c: dict[str, float] = {}
    notes: list[str] = []
    best = max(events, key=lambda e: (e.materiality or 0.0, e.credibility or 0.0))

    c["base"] = 40.0 + 25.0 * prior.weight
    c["materiality"] = round(((best.materiality or 0.6) - 0.6) * 25.0, 2)
    novelty = max((e.novelty if e.novelty is not None else 1.0) for e in events)
    c["novelty"] = 5.0 if novelty >= 1.0 else -10.0
    cred = max((e.credibility or 0.7) for e in events)
    c["credibility"] = round((cred - 0.7) * 30.0, 2)
    if any(e.strength == "strong" for e in events):
        c["strength"] = 6.0
    origins = {e.origin for e in events} | {e.source_key for e in events if e.source_key}
    if len(events) > 1:
        c["corroboration"] = min(6.0, 3.0 * (len(origins) - 1)) if len(origins) > 1 else 0.0

    margins = [e.sentiment.get("pos", 0) - e.sentiment.get("neg", 0) for e in events if e.sentiment]
    disagrees = any(_model_disagrees(e) for e in events)
    if disagrees:
        c["model_disagrees"] = -12.0
        notes.append("Sentiment model disagrees with the rule (rule kept; confidence lowered).")
    elif margins:
        c["sentiment"] = round(max(margins) * 8.0, 2)

    skip = None
    if f is not None:
        if f.volume_ratio is not None and f.volume_ratio > 1.5:
            c["volume_confirmation"] = 5.0
        if f.above_sma50 is not None:
            c["trend"] = 4.0 if f.above_sma50 else -4.0
        if f.adv20_usd is not None and f.adv20_usd < MIN_ADV_USD:
            c["liquidity"] = -15.0
            notes.append(f"Thin trading: ${f.adv20_usd / 1e6:.1f}M average daily volume.")
        if f.reaction_pct is not None:
            if priced_in(f):
                skip = "priced_in"
                notes.append(f"Already up {f.reaction_pct:.1f}% since the news "
                             f"({f.reaction_atr or 0:.1f}x its daily range): likely priced in.")
            elif f.reaction_atr is not None and f.reaction_atr < -1.0:
                c["reaction"] = -8.0
                notes.append(f"Market reacted negatively ({f.reaction_pct:.1f}% since the news).")
            elif f.reaction_pct > 0:
                c["reaction"] = 3.0
        if f.atr20_pct is not None and f.atr20_pct > 6:
            notes.append(f"Very volatile: average daily range {f.atr20_pct:.1f}%.")
    else:
        notes.append("No price history yet: stop and size are rough.")
    if prior.catalyst == "upgrade":
        notes.append("Analyst upgrades are a low-weight catalyst.")

    total = max(0.0, min(99.0, sum(c.values())))
    return RuleScore(round(total, 1), c, skip, notes, disagrees)
