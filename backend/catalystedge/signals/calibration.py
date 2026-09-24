"""Live calibrator: maps a rule confidence to an observed hit probability, but ONLY when a
backtest calibration snapshot for that family (or "all") has verdict good or fair. Otherwise
it returns None and the signal stays labelled UNCALIBRATED (rule 6)."""

from __future__ import annotations

import bisect

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.db.models import CalibrationSnapshot


class SnapshotCalibrator:
    def __init__(self, session: Session):
        self._maps: dict[str, tuple[int, list[list[float]]]] = {}
        rows = session.scalars(select(CalibrationSnapshot).where(
            CalibrationSnapshot.basis == "backtest", CalibrationSnapshot.verdict.in_(("good", "fair")))
            .order_by(CalibrationSnapshot.created_at)).all()
        for r in rows:
            mapping = next((b["mapping"] for b in r.buckets if isinstance(b, dict) and "mapping" in b), None)
            if mapping:
                self._maps[r.event_family] = (r.id, mapping)

    @property
    def active(self) -> bool:
        return bool(self._maps)

    def calibrate(self, catalyst: str, raw_confidence: float) -> tuple[float, int] | None:
        found = self._maps.get(catalyst) or self._maps.get("all")
        if not found:
            return None
        snap_id, mapping = found
        xs = [m[0] for m in mapping]
        i = bisect.bisect_right(xs, raw_confidence) - 1
        if i < 0:
            p = mapping[0][1]
        elif i >= len(mapping) - 1:
            p = mapping[-1][1]
        else:
            (x0, y0), (x1, y1) = mapping[i], mapping[i + 1]
            p = y0 + (y1 - y0) * ((raw_confidence - x0) / (x1 - x0) if x1 > x0 else 0)
        return round(max(0.0, min(99.0, p * 100)), 1), snap_id
