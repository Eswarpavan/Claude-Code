"""One feature vector for both the live engine and the backtest/ranker (no train/serve skew).

Input is the `features` dict the engine stores on each signal (rule components, price
features, catalyst, origins, TimesFM tag). Missing values are NaN; LightGBM handles them.
"""

from __future__ import annotations

import math

from catalystedge.signals.priors import CATALYSTS

ORIGINS = ("news", "filing", "earnings", "fda")
COMPONENTS = ("materiality", "novelty", "credibility", "sentiment", "model_disagrees", "strength",
              "volume_confirmation", "trend", "liquidity", "reaction", "corroboration")
PRICE = ("atr20_pct", "volume_ratio", "mom5_pct", "mom20_pct", "mom60_pct", "reaction_pct", "reaction_atr", "gap_pct")

FEATURES: tuple[str, ...] = (
    ("rule_score", "catalyst_weight", "n_events", "log_adv20", "above_sma50", "timesfm_er", "timesfm_width")
    + tuple(f"c_{c}" for c in COMPONENTS) + PRICE
    + tuple(f"is_{c}" for c in CATALYSTS) + tuple(f"from_{o}" for o in ORIGINS)
)
NAN = float("nan")


def _num(x) -> float:
    if x is None or isinstance(x, bool):
        return NAN if x is None else float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return NAN


def vectorize(feats: dict) -> list[float]:
    comps = feats.get("rule_components") or {}
    price = feats.get("price") or {}
    tfm = (feats.get("timesfm") or {}).get("forecast") or {}
    adv = _num(price.get("adv20_usd"))
    row = {
        "rule_score": _num(feats.get("rule_score")),
        "catalyst_weight": _num(feats.get("catalyst_weight")),
        "n_events": _num(feats.get("n_events")),
        "log_adv20": math.log10(adv) if adv == adv and adv > 0 else NAN,
        "above_sma50": _num(price.get("above_sma50")),
        "timesfm_er": _num(tfm.get("expected_return_pct")),
        "timesfm_width": (_num(tfm.get("high_pct")) - _num(tfm.get("low_pct"))) if tfm else NAN,
    }
    for c in COMPONENTS:
        row[f"c_{c}"] = _num(comps.get(c, 0.0))
    for p in PRICE:
        row[p] = _num(price.get(p))
    cat = feats.get("catalyst")
    for c in CATALYSTS:
        row[f"is_{c}"] = 1.0 if cat == c else 0.0
    origins = set(feats.get("origins") or [])
    for o in ORIGINS:
        row[f"from_{o}"] = 1.0 if o in origins else 0.0
    return [row[f] for f in FEATURES]
