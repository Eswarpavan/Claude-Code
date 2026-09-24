"""How a TimesFM forecast may change a signal (the model itself lives in ml/timeseries.py).

OFF (default): nothing changes. "feature": the forecast nudges confidence by at most
+/- MAX_EFFECT points (until the ranker learns its weight). "filter": a signal is kept
only if the 3-10 day forecast is positive; removed ones are listed with reasons.
A missing or failed forecast never blocks a signal: the normal pipeline result stands
and a warning is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

MODES = ("feature", "filter")
MAX_EFFECT = 5.0


@dataclass(frozen=True)
class TimesFMState:
    enabled: bool = False
    mode: str = "feature"

    def tag(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode if self.enabled else None}


@dataclass(frozen=True)
class Forecast:
    symbol: str
    horizon_days: int
    expected_return_pct: float      # median forecast close at horizon vs last close
    low_pct: float                  # 10th percentile
    high_pct: float                 # 90th percentile
    model: str
    as_of: str                      # ISO date of the last input bar


@dataclass(frozen=True)
class Effect:
    confidence_delta: float = 0.0
    filtered: bool = False
    note: str | None = None
    warning: str | None = None
    forecast: Forecast | None = None

    def as_dict(self, state: TimesFMState) -> dict:
        d = {**state.tag(), "confidence_delta": self.confidence_delta, "filtered": self.filtered,
             "note": self.note, "warning": self.warning}
        if self.forecast:
            f = self.forecast
            d["forecast"] = {"expected_return_pct": round(f.expected_return_pct, 3), "low_pct": round(f.low_pct, 3),
                             "high_pct": round(f.high_pct, 3), "horizon_days": f.horizon_days, "model": f.model,
                             "as_of": f.as_of}
        return d


def apply(state: TimesFMState, forecast: Forecast | None, status: str = "ready") -> Effect:
    if not state.enabled:
        return Effect()
    if forecast is None:
        return Effect(warning=f"TimesFM {status}: no forecast for this ticker, normal pipeline result used")
    er, width = forecast.expected_return_pct, max(0.5, forecast.high_pct - forecast.low_pct)
    if state.mode == "filter":
        if er <= 0:
            return Effect(filtered=True, forecast=forecast,
                          note=f"TimesFM forecasts {er:+.2f}% over {forecast.horizon_days} days (not positive)")
        return Effect(forecast=forecast, note=f"TimesFM forecast {er:+.2f}% supports the signal")
    # feature mode: signal-to-noise of the forecast, bounded
    delta = max(-MAX_EFFECT, min(MAX_EFFECT, 2.0 * er / width * MAX_EFFECT))
    return Effect(confidence_delta=round(delta, 2), forecast=forecast,
                  note=f"TimesFM {er:+.2f}% (range {forecast.low_pct:+.1f}..{forecast.high_pct:+.1f}%): "
                       f"confidence {delta:+.1f}")
