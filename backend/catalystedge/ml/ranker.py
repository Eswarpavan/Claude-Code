"""Live LightGBM ranker (Stage 2). Used by the signal engine ONLY when the model registry
says it is enabled, which happens only after it beat every baseline out of sample."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from catalystedge.signals.vector import FEATURES, vectorize

RANKER_NAME = "ranker-lgbm"


class LgbRanker:
    def __init__(self, path: Path):
        import lightgbm as lgb

        self.meta = json.loads((path / "meta.json").read_text())
        if self.meta["features"] != list(FEATURES):
            raise ValueError("ranker was trained on a different feature set; retrain with `catalystedge backtest`")
        self.booster = lgb.Booster(model_file=str(path / "model.txt"))
        self.weight = float(self.meta.get("weight", 0.5))
        self.version = f"lgbm-{self.meta['trained_at'][:10]}"

    def predict_proba(self, features: dict) -> float:
        return float(self.booster.predict(np.array([vectorize(features)], dtype=float))[0])

    def explain(self, features: dict, top: int = 5) -> dict:
        """SHAP values (LightGBM's exact TreeSHAP via pred_contrib): top contributions in log-odds."""
        contrib = self.booster.predict(np.array([vectorize(features)], dtype=float), pred_contrib=True)[0]
        pairs = sorted(zip(FEATURES, contrib[:-1], strict=True), key=lambda kv: -abs(kv[1]))[:top]
        return {"base": float(contrib[-1]), "top": [{"feature": f, "contribution": round(float(v), 4),
                                                     "plain": _plain(f, float(v))} for f, v in pairs]}


def _plain(feature: str, value: float) -> str:
    names = {"rule_score": "rule score", "reaction_pct": "price move since the news", "mom20_pct": "20-day momentum",
             "mom60_pct": "60-day momentum", "atr20_pct": "volatility", "volume_ratio": "volume surge",
             "log_adv20": "liquidity", "timesfm_er": "TimesFM forecast", "gap_pct": "opening gap after the news"}
    name = names.get(feature, feature.replace("c_", "").replace("is_", "catalyst: ").replace("from_", "source: ")
                     .replace("_", " "))
    return f"{name} {'raised' if value > 0 else 'lowered'} the model's estimate"


def load_enabled_ranker(session, models_dir: Path) -> LgbRanker | None:
    """The ranker if the registry marks it enabled (beat baselines) and the files load; else None."""
    from sqlalchemy import select

    from catalystedge.db.models import ModelRegistryRow

    row = session.scalar(select(ModelRegistryRow).where(ModelRegistryRow.name == RANKER_NAME))
    if row is None or not row.enabled or not row.local_path:
        return None
    try:
        return LgbRanker(Path(row.local_path))
    except Exception:
        return None
