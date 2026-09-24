"""Model registry: config-driven choice, automatic first-run download, fallbacks,
and a status report for the health check and the Source Health page.

Candidates (docs/ARCHITECTURE.md §11). Which one becomes the default is decided
by `catalystedge eval-sentiment` on your machine; until then FinBERT is used.
Any model that is not shown to help in validation is kept disabled later
(Phase 2); this registry only handles availability.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from catalystedge.config import Settings
from catalystedge.ml.sentiment import HuggingFaceSentiment, LexiconSentiment, SentimentModel

READY_MARKER = ".catalystedge_ready"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    kind: str
    approx_mb: int
    note: str


SENTIMENT_MODELS: dict[str, ModelSpec] = {
    "finbert": ModelSpec("finbert", "ProsusAI/finbert", "sentiment", 440,
                         "BERT-base fine-tuned on Financial PhraseBank; the most widely used open model"),
    "distilroberta": ModelSpec("distilroberta", "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis",
                               "sentiment", 330, "about 2x faster on CPU"),
    "deberta": ModelSpec("deberta", "mrm8488/deberta-v3-ft-financial-news-sentiment-analysis", "sentiment", 740,
                         "DeBERTa-v3 fine-tune; slower, sometimes more accurate"),
}


@dataclass
class ModelStatus:
    name: str
    repo: str
    status: str          # ready | missing | downloading | failed | unavailable
    detail: str
    local_path: str | None = None


def ml_extra_installed() -> bool:
    return importlib.util.find_spec("transformers") is not None and importlib.util.find_spec("torch") is not None


def _default_download(spec: ModelSpec, target: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id=spec.repo, local_dir=target,
                      allow_patterns=["*.json", "*.safetensors", "*.bin", "*.txt", "*.model", "vocab*"])


def _default_load(spec: ModelSpec, path: Path) -> SentimentModel:
    from transformers import AutoConfig, pipeline

    config = AutoConfig.from_pretrained(path)
    pipe = pipeline("text-classification", model=str(path), tokenizer=str(path), top_k=None, truncation=True,
                    max_length=64, device=-1)
    return HuggingFaceSentiment(spec.name, pipe, id2label={int(k): v for k, v in config.id2label.items()})


class ModelRegistry:
    def __init__(self, settings: Settings,
                 download: Callable[[ModelSpec, Path], None] = _default_download,
                 load: Callable[[ModelSpec, Path], SentimentModel] = _default_load,
                 ml_available: Callable[[], bool] = ml_extra_installed):
        self.settings = settings
        self._download = download
        self._load = load
        self._ml_available = ml_available
        self.statuses: dict[str, ModelStatus] = {}
        self._cache: dict[str, SentimentModel] = {}

    def path(self, name: str) -> Path:
        return Path(self.settings.models_dir) / name

    def status(self, name: str) -> ModelStatus:
        spec = SENTIMENT_MODELS[name]
        path = self.path(name)
        if name in self.statuses:
            return self.statuses[name]
        if (path / READY_MARKER).exists():
            return ModelStatus(name, spec.repo, "ready", "downloaded", str(path))
        if not self._ml_available():
            return ModelStatus(name, spec.repo, "unavailable",
                               "ML libraries not installed (install with: uv sync --extra ml)")
        return ModelStatus(name, spec.repo, "missing", "not downloaded yet")

    def ensure(self, name: str) -> ModelStatus:
        """Download the model if needed and allowed. Never raises."""
        spec = SENTIMENT_MODELS[name]
        st = self.status(name)
        if st.status in ("ready", "unavailable"):
            return st
        if not self.settings.model_auto_download:
            return ModelStatus(name, spec.repo, "missing", "auto-download is off (MODEL_AUTO_DOWNLOAD=false)")
        path = self.path(name)
        self.statuses[name] = ModelStatus(name, spec.repo, "downloading", f"~{spec.approx_mb} MB, first run only")
        try:
            path.mkdir(parents=True, exist_ok=True)
            self._download(spec, path)
            (path / READY_MARKER).write_text(spec.repo)
            self.statuses[name] = ModelStatus(name, spec.repo, "ready", "downloaded", str(path))
        except Exception as e:
            self.statuses[name] = ModelStatus(name, spec.repo, "failed", f"download failed: {type(e).__name__}: {e}")
        return self.statuses[name]

    def sentiment(self) -> SentimentModel:
        """The configured model if it can be made ready, else the next candidate,
        else the word-list fallback. Never raises."""
        wanted = self.settings.sentiment_model
        if wanted == "lexicon":
            return LexiconSentiment()
        # Configured model first, then FinBERT; never download every candidate.
        order = list(dict.fromkeys([wanted, "finbert"])) if wanted in SENTIMENT_MODELS else ["finbert"]
        for name in order:
            if name in self._cache:
                return self._cache[name]
            st = self.ensure(name)
            if st.status != "ready":
                continue
            try:
                model = self._load(SENTIMENT_MODELS[name], self.path(name))
            except Exception as e:
                self.statuses[name] = ModelStatus(name, st.repo, "failed", f"load failed: {type(e).__name__}: {e}")
                continue
            self._cache[name] = model
            return model
        return LexiconSentiment()

    def report(self) -> list[ModelStatus]:
        return [self.status(n) for n in SENTIMENT_MODELS]
