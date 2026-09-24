"""Financial sentiment models behind one interface.

HuggingFaceSentiment wraps a local transformers text-classification model
(FinBERT and alternatives; see registry.py). LexiconSentiment is a small,
transparent word-list fallback used when no model is available, e.g. before
the first download or in the offline build sandbox. Its scores are always
labelled "lexicon-fallback" so they are never mistaken for FinBERT output.

Sentiment is one input to event classification, never the verdict on its own.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SentimentScore:
    positive: float
    neutral: float
    negative: float
    model: str

    @property
    def label(self) -> str:
        # Equal positive and negative evidence is not a positive (or negative) headline.
        if abs(self.positive - self.negative) < 1e-9:
            return "neutral"
        return max((("positive", self.positive), ("neutral", self.neutral), ("negative", self.negative)),
                   key=lambda kv: kv[1])[0]

    @property
    def margin(self) -> float:
        """positive minus negative, in -1..1."""
        return self.positive - self.negative

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "pos": round(self.positive, 4), "neu": round(self.neutral, 4),
                "neg": round(self.negative, 4), "label": self.label}


class SentimentModel(Protocol):
    name: str

    def predict(self, texts: Sequence[str]) -> list[SentimentScore]: ...


# ----------------------------------------------------------------------------- Hugging Face models


def normalize_label(label: str, id2label: dict[int, str] | None = None) -> str:
    """Map model-specific labels (positive / LABEL_2 / Bullish ...) to positive|neutral|negative."""
    raw = label
    m = re.fullmatch(r"LABEL_(\d+)", label)
    if m and id2label:
        raw = id2label.get(int(m.group(1)), label)
    low = raw.lower()
    if low.startswith(("pos", "bull")):
        return "positive"
    if low.startswith(("neg", "bear")):
        return "negative"
    if low.startswith("neu"):
        return "neutral"
    raise ValueError(f"unrecognised sentiment label {label!r}")


class HuggingFaceSentiment:
    """`pipeline` is a callable like transformers.pipeline("text-classification", top_k=None):
    given a list of texts it returns, per text, a list of {label, score}."""

    def __init__(self, name: str, pipeline: Callable[..., Any], id2label: dict[int, str] | None = None,
                 batch_size: int = 32):
        self.name = name
        self._pipeline = pipeline
        self._id2label = id2label
        self.batch_size = batch_size

    def predict(self, texts: Sequence[str]) -> list[SentimentScore]:
        out: list[SentimentScore] = []
        for i in range(0, len(texts), self.batch_size):
            batch = list(texts[i : i + self.batch_size])
            for per_text in self._pipeline(batch):
                probs = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
                for row in per_text:
                    probs[normalize_label(row["label"], self._id2label)] += float(row["score"])
                total = sum(probs.values()) or 1.0
                out.append(SentimentScore(probs["positive"] / total, probs["neutral"] / total,
                                          probs["negative"] / total, self.name))
        return out


# ----------------------------------------------------------------------------- word-list fallback

POSITIVE = frozenset(
    "beat beats tops topped exceeds exceeded surpasses raises raised raise record upgrade upgraded upgrades "
    "approval approves approved wins won awarded award surges surge jumps jump soars soar rallies rally climbs "
    "gains gain strong stronger growth outperform bullish boost boosts expands expansion higher profit "
    "profitable breakthrough positive meet meets met approve accelerates accelerating robust upbeat optimistic "
    "buyback".split()
)
NEGATIVE = frozenset(
    "miss misses missed cuts cut lowers lowered downgrade downgraded downgrades falls fall slips slip drops drop "
    "plunges plunge sinks sink tumbles tumble weak weaker loss losses lawsuit probe investigation recall halt "
    "halts halted delay delays delayed reject rejects rejected rejection warning warns withdraws withdrawn bankruptcy "
    "layoffs fraud dilution dilutive offering declines decline slump slumps disappoints disappointing "
    "downbeat bearish suspends suspended fails failed crl".split()
)
NEGATORS = frozenset("not no never without fails fail failed".split())
WORD = re.compile(r"[a-z]+")


class LexiconSentiment:
    name = "lexicon-fallback"

    def predict(self, texts: Sequence[str]) -> list[SentimentScore]:
        return [self._score(t) for t in texts]

    def _score(self, text: str) -> SentimentScore:
        words = WORD.findall(text.lower())
        pos = neg = 0.0
        for i, w in enumerate(words):
            negated = any(x in NEGATORS for x in words[max(0, i - 3) : i])
            if w in POSITIVE:
                if negated:
                    neg += 1
                else:
                    pos += 1
            elif w in NEGATIVE and w not in NEGATORS:
                if negated:   # "did not reject" reads as mildly positive
                    pos += 1
                else:
                    neg += 1
            elif w in NEGATORS and w in {"fails", "failed", "fail"}:
                neg += 1
        if "complete response letter" in text.lower():
            neg += 2
        neutral_prior = 0.8   # below 1 so a single clear cue outweighs "no information"
        total = pos + neutral_prior + neg
        return SentimentScore(pos / total, neutral_prior / total, neg / total, self.name)
