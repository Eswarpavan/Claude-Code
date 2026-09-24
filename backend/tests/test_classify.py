"""Step 6: event classification, including explicit mixed-headline handling."""

import pytest

from catalystedge.fixtures import load_json
from catalystedge.ml.sentiment import LexiconSentiment, SentimentScore
from catalystedge.pipeline.classify import classify, materiality
from catalystedge.pipeline.ticker_link import Mention, Universe, link_tickers

U = Universe.from_sec_company_tickers(load_json("reference/company_tickers.json"))
LEX = LexiconSentiment()


def run(headline, provider=None, sentiment=None):
    mentions = link_tickers(headline, provider or {}, U).mentions
    s = sentiment if sentiment is not None else LEX.predict([headline])[0]
    return {e.symbol: e for e in classify(headline, mentions, s)}


def subject(headline):
    """For fictional companies: pretend the first word is a linked ticker."""
    return [Mention("ACME", "name", 0.9, 0, len(headline.split()[0]), is_primary=True)]


@pytest.mark.parametrize(
    ("headline", "symbol", "event_type", "polarity"),
    [
        ("NVIDIA beats estimates and raises full-year revenue guidance", "NVDA", "guidance_raise", "positive"),
        ("Allstate (NYSE: ALL) beats quarterly profit estimates on lower catastrophe losses", "ALL",
         "earnings_beat", "positive"),
        ("Lockheed Martin wins $2.1 billion Pentagon contract for missile defense", "LMT", "contract_win", "positive"),
        ("Vertex Pharmaceuticals pain drug meets primary endpoint in Phase 3 trial", "VRTX", "positive_trial",
         "positive"),
        ("Apple upgraded to Buy at Example Securities on services growth", "AAPL", "upgrade", "positive"),
        ("Palantir awarded $480 million Army contract", "PLTR", "contract_win", "positive"),
        ("Gartner (NYSE: IT) raises annual outlook after strong quarter", "IT", "guidance_raise", "positive"),
        ("FDA approves Madrigal liver drug for broader patient group", "MDGL", "fda_approval", "positive"),
        ("$ON jumps after ON Semiconductor raises guidance on EV demand", "ON", "guidance_raise", "positive"),
        ("CrowdStrike beats estimates and raises annual guidance", "CRWD", "guidance_raise", "positive"),
        # negative / mixed / no catalyst
        ("Intel beats estimates but cuts fourth-quarter guidance", "INTC", "earnings_beat", "mixed"),
        ("FDA approves Sarepta gene therapy with boxed warning and narrower label", "SRPT", "fda_approval", "mixed"),
        ("Moderna shares slip after FDA issues complete response letter for flu vaccine", "MRNA", "other",
         "negative"),
        ("ServiceNow shares fall after downgrade to Neutral at Example Bank", "NOW", "other", "negative"),
        ("Alphabet announces $70 billion share buyback", "GOOGL", "other", "positive"),
    ],
)
def test_fixture_headlines(headline, symbol, event_type, polarity):
    e = run(headline)[symbol]
    assert (e.event_type, e.polarity) == (event_type, polarity), e.reasons


def test_beat_and_raise_is_strong():
    e = run("NVIDIA beats estimates and raises full-year revenue guidance")["NVDA"]
    assert e.strength == "strong"
    assert "earnings_beat" in " ".join(e.reasons) and "guidance_raise" in " ".join(e.reasons)


def test_acquisition_target_positive_acquirer_not():
    events = run("Abbott to acquire Hologic for $15 billion in cash")
    assert (events["HOLX"].event_type, events["HOLX"].polarity) == ("m_and_a_target", "positive")
    assert events["ABT"].event_type == "other" and not events["ABT"].is_signal_eligible
    assert any("acquirer" in r for r in events["ABT"].reasons)


def test_passive_acquisition_marks_subject_as_target():
    events = run("Hologic agrees to be acquired by Abbott at 30% premium")
    assert events["HOLX"].event_type == "m_and_a_target"
    assert events["HOLX"].materiality >= 0.9


def test_buy_rating_is_not_an_acquisition():
    e = run("Apple upgraded to Buy at Example Securities")["AAPL"]
    assert e.event_type == "upgrade" and not any("acquirer" in r for r in e.reasons)


def test_every_labelled_mixed_headline_is_not_clean_positive():
    mixed = [r["headline"] for r in load_json("reference/labeled_headlines.json")["items"] if r["mixed"]]
    assert len(mixed) == 10
    for h in mixed:
        (e,) = classify(h, subject(h), LEX.predict([h])[0])
        assert e.polarity != "positive", (h, e)
        assert not e.is_signal_eligible


def test_mixed_resolution_explains_itself():
    e = run("Intel beats estimates but cuts fourth-quarter guidance")["INTC"]
    assert e.mixed_resolution["conflict"].startswith("guidance cut")
    assert e.mixed_resolution["clauses"] == ["Intel beats estimates", "cuts fourth-quarter guidance"]


def test_strongly_negative_model_vetoes_rule_positive():
    veto = SentimentScore(0.1, 0.2, 0.7, "finbert")
    e = run("Palantir awarded $480 million Army contract", sentiment=veto)["PLTR"]
    assert e.polarity == "mixed" and "model" in e.mixed_resolution["conflict"]


def test_model_alone_never_creates_a_catalyst():
    glowing = SentimentScore(0.95, 0.04, 0.01, "finbert")
    e = run("Alphabet announces $70 billion share buyback", sentiment=glowing)["GOOGL"]
    assert e.event_type == "other" and not e.is_signal_eligible


def test_catalyst_belongs_to_subject_not_every_mention():
    h = "Apple supplier Palantir wins $480 million Army contract"
    events = run(h)
    assert events["AAPL"].event_type == "other" and events["AAPL"].polarity == "neutral"
    assert (events["PLTR"].event_type, events["PLTR"].polarity) == ("contract_win", "positive")
    assert any("belongs to PLTR" in r for r in events["AAPL"].reasons)


def test_upgrade_with_target_cut_is_mixed():
    (e,) = classify("Acme upgraded to Buy but price target lowered", subject("Acme"), None)
    assert e.polarity == "mixed" and "price target" in e.mixed_resolution["conflict"]


def test_downgrade_is_not_an_upgrade():
    (e,) = classify("Acme downgraded to Sell", subject("Acme"), None)
    assert e.event_type == "other" and e.polarity == "negative"


def test_trial_failure_is_negative_not_positive_trial():
    (e,) = classify("Acme trial fails to meet primary endpoint", subject("Acme"), None)
    assert e.event_type == "other" and e.polarity == "negative"


@pytest.mark.parametrize(
    ("headline", "cap", "expected"),
    [
        ("wins $2.1 billion contract", None, 0.9),
        ("wins $480 million contract", None, 0.7),
        ("wins $20 million contract", None, 0.45),
        ("wins $480 million contract", 3e9, 0.95),      # 16% of market cap
        ("wins $480 million contract", 500e9, 0.35),    # 0.1% of market cap
        ("raises guidance", None, 0.6),
        ("to be acquired at 35% premium", None, 0.9),
    ],
)
def test_materiality(headline, cap, expected):
    assert materiality(headline, cap) == expected
