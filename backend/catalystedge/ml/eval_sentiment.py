"""Compare sentiment models on hand-labelled headlines (accuracy, macro-F1, speed).

Run on your machine after `uv sync --extra ml`:  catalystedge eval-sentiment
The labelled set lives in catalystedge/fixtures/reference/labeled_headlines.json
(hand-labelled by CatalystEdge's author; correct it as you see fit).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from catalystedge.fixtures import load_json
from catalystedge.ml.sentiment import SentimentModel

LABELS = ("positive", "neutral", "negative")


@dataclass
class EvalResult:
    model: str
    n: int
    accuracy: float
    macro_f1: float
    per_label_f1: dict[str, float]
    ms_per_headline: float


def labelled_headlines() -> list[tuple[str, str]]:
    """Unambiguous items only; mixed headlines are handled by the rules in classify.py."""
    items = load_json("reference/labeled_headlines.json")["items"]
    return [(r["headline"], r["label"]) for r in items if not r["mixed"]]


def evaluate(model: SentimentModel, data: Sequence[tuple[str, str]] | None = None) -> EvalResult:
    data = list(data or labelled_headlines())
    start = time.perf_counter()
    preds = [s.label for s in model.predict([h for h, _ in data])]
    elapsed = time.perf_counter() - start
    gold = [y for _, y in data]
    f1: dict[str, float] = {}
    for label in LABELS:
        tp = sum(p == label and g == label for p, g in zip(preds, gold, strict=True))
        fp = sum(p == label and g != label for p, g in zip(preds, gold, strict=True))
        fn = sum(p != label and g == label for p, g in zip(preds, gold, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1[label] = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = sum(p == g for p, g in zip(preds, gold, strict=True)) / len(gold)
    return EvalResult(model.name, len(gold), accuracy, sum(f1.values()) / len(LABELS), f1,
                      1000 * elapsed / max(1, len(gold)))
