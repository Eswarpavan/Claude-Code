"""Step 5: sentiment models, registry and scoring."""

import datetime as dt

import pytest

from catalystedge.config import Settings
from catalystedge.ml.eval_sentiment import evaluate, labelled_headlines
from catalystedge.ml.registry import READY_MARKER, SENTIMENT_MODELS, ModelRegistry
from catalystedge.ml.sentiment import HuggingFaceSentiment, LexiconSentiment, SentimentScore, normalize_label
from catalystedge.pipeline.score import score_headlines
from catalystedge.pipeline.window import StaleNewsError

NOW = dt.datetime(2026, 9, 24, 13, 30, tzinfo=dt.UTC)


# ----------------------------------------------------------------------------- label handling


@pytest.mark.parametrize(
    ("label", "id2label", "expected"),
    [
        ("positive", None, "positive"), ("Negative", None, "negative"), ("neutral", None, "neutral"),
        ("LABEL_0", {0: "negative", 1: "neutral", 2: "positive"}, "negative"),
        ("LABEL_2", {0: "negative", 1: "neutral", 2: "positive"}, "positive"),
        ("Bullish", None, "positive"),
    ],
)
def test_normalize_label(label, id2label, expected):
    assert normalize_label(label, id2label) == expected


def test_unknown_label_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        normalize_label("happy")


def fake_finbert(texts):
    """Mimics transformers text-classification with top_k=None (FinBERT label names)."""
    out = []
    for t in texts:
        pos = 0.9 if "beats" in t else 0.05
        neg = 0.9 if "misses" in t else 0.05
        out.append([{"label": "positive", "score": pos}, {"label": "negative", "score": neg},
                    {"label": "neutral", "score": 1 - pos - neg if pos + neg < 1 else 0.05}])
    return out


def test_huggingface_wrapper_batches_and_normalises():
    calls = []

    def pipe(batch):
        calls.append(len(batch))
        return fake_finbert(batch)

    m = HuggingFaceSentiment("finbert", pipe, batch_size=2)
    scores = m.predict(["Acme beats", "Acme misses", "Acme files report"])
    assert calls == [2, 1]
    assert [s.label for s in scores] == ["positive", "negative", "neutral"]
    assert all(abs(s.positive + s.neutral + s.negative - 1) < 1e-9 for s in scores)
    assert scores[0].model == "finbert"


# ----------------------------------------------------------------------------- registry


def reg(tmp_path, *, ml=True, auto=True, download=None, load=None, model="finbert"):
    settings = Settings(_env_file=None, MODELS_DIR=str(tmp_path), MODEL_AUTO_DOWNLOAD=str(auto).lower(),
                        SENTIMENT_MODEL=model)
    downloads = []

    def default_download(spec, path):
        downloads.append(spec.name)
        (path / "config.json").write_text("{}")

    def default_load(spec, path):
        return HuggingFaceSentiment(spec.name, fake_finbert)

    r = ModelRegistry(settings, download=download or default_download, load=load or default_load,
                      ml_available=lambda: ml)
    return r, downloads


def test_downloads_once_on_first_use_then_uses_the_model(tmp_path):
    r, downloads = reg(tmp_path)
    assert r.status("finbert").status == "missing"
    assert r.sentiment().name == "finbert"
    assert (tmp_path / "finbert" / READY_MARKER).exists()
    r2, downloads2 = reg(tmp_path)
    assert r2.sentiment().name == "finbert"
    assert downloads == ["finbert"] and downloads2 == []   # cached in the models volume


def test_without_ml_libraries_falls_back_to_labelled_lexicon(tmp_path):
    r, downloads = reg(tmp_path, ml=False)
    m = r.sentiment()
    assert isinstance(m, LexiconSentiment) and m.name == "lexicon-fallback"
    assert r.status("finbert").status == "unavailable" and "uv sync --extra ml" in r.status("finbert").detail
    assert downloads == []


def test_auto_download_off_means_no_network(tmp_path):
    r, downloads = reg(tmp_path, auto=False)
    assert r.sentiment().name == "lexicon-fallback"
    assert downloads == []


def test_failed_download_is_reported_and_falls_back(tmp_path):
    def broken(spec, path):
        raise ConnectionError("host blocked")

    r, _ = reg(tmp_path, download=broken)
    assert r.sentiment().name == "lexicon-fallback"
    st = r.status("finbert")
    assert st.status == "failed" and "host blocked" in st.detail


def test_configured_alternative_then_finbert_only(tmp_path):
    attempted = []

    def download(spec, path):
        attempted.append(spec.name)
        raise ConnectionError("nope")

    r, _ = reg(tmp_path, download=download, model="deberta")
    r.sentiment()
    assert attempted == ["deberta", "finbert"]   # never tries every candidate


def test_broken_model_files_fall_back(tmp_path):
    def bad_load(spec, path):
        raise OSError("corrupt weights")

    r, _ = reg(tmp_path, load=bad_load)
    assert r.sentiment().name == "lexicon-fallback"
    assert r.status("finbert").status == "failed"


def test_every_registered_model_has_a_repo():
    assert set(SENTIMENT_MODELS) == {"finbert", "distilroberta", "deberta"}
    assert all("/" in s.repo for s in SENTIMENT_MODELS.values())


# ----------------------------------------------------------------------------- lexicon fallback


@pytest.mark.parametrize(
    ("headline", "label"),
    [
        ("Acme beats estimates and raises guidance", "positive"),
        ("Acme cuts guidance on weak demand", "negative"),
        ("Acme to report earnings on Thursday", "neutral"),
        ("Stark Biotech trial fails to meet primary endpoint", "negative"),
        ("FDA issues complete response letter to Umbrella", "negative"),
        ("Regulator did not reject the application", "positive"),
    ],
)
def test_lexicon_basics(headline, label):
    assert LexiconSentiment().predict([headline])[0].label == label


def test_scores_are_probabilities_and_margin():
    s = LexiconSentiment().predict(["Acme beats and raises"])[0]
    assert abs(s.positive + s.neutral + s.negative - 1) < 1e-9
    assert s.margin == pytest.approx(s.positive - s.negative)
    assert s.as_dict()["model"] == "lexicon-fallback"


# ----------------------------------------------------------------------------- scoring + evaluation


def test_scoring_enforces_the_48h_window():
    fresh = ("Acme beats", NOW - dt.timedelta(hours=2))
    stale = ("Acme beats", NOW - dt.timedelta(hours=49))
    assert len(score_headlines(LexiconSentiment(), [fresh], NOW)) == 1
    with pytest.raises(StaleNewsError):
        score_headlines(LexiconSentiment(), [fresh, stale], NOW)


def test_eval_harness_metrics_are_correct():
    class AlwaysPositive:
        name = "always-positive"

        def predict(self, texts):
            return [SentimentScore(1, 0, 0, self.name) for _ in texts]

    data = [("a", "positive"), ("b", "negative"), ("c", "neutral"), ("d", "positive")]
    r = evaluate(AlwaysPositive(), data)
    assert r.accuracy == 0.5
    assert r.per_label_f1["positive"] == pytest.approx(2 / 3)
    assert r.macro_f1 == pytest.approx((2 / 3) / 3)


def test_labelled_set_is_balanced_and_excludes_mixed():
    data = labelled_headlines()
    counts = {lab: sum(y == lab for _, y in data) for lab in ("positive", "neutral", "negative")}
    assert len(data) == 50 and min(counts.values()) >= 14


def test_lexicon_fallback_quality_floor():
    """Regression guard only; the real model is chosen by eval-sentiment on your machine."""
    r = evaluate(LexiconSentiment())
    assert r.macro_f1 >= 0.70, r
