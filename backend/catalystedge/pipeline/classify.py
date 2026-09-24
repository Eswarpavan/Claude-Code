"""Event classification: headline + linked tickers + sentiment -> catalyst type and polarity.

Deterministic phrase rules decide the catalyst and its polarity; the sentiment
model only cross-checks (a strongly negative model score turns a rule-positive
headline into "mixed"). Mixed headlines are handled explicitly:

  * a guidance cut / withdrawal anywhere overrides any beat           -> mixed
  * approval with a boxed warning or narrower label                   -> mixed
  * upgrade with a lowered price target                               -> mixed
  * beat on one line, miss on another                                 -> mixed
  * any positive catalyst next to any negative cue                    -> mixed

Only "positive" events with a catalyst type other than "other" can ever feed a
signal (hard rule 3). Everything is still returned so it can be logged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from catalystedge.ml.sentiment import SentimentScore
from catalystedge.pipeline.ticker_link import Mention

CLASSIFIER_VERSION = "rules-v2"

# Priority when one headline carries several positive catalysts.
PRIORITY = ("fda_approval", "positive_trial", "m_and_a_target", "guidance_raise", "earnings_beat", "contract_win",
            "upgrade", "insider_buy_cluster")

_ESTIMATES = r"(?:estimates?|expectations?|forecasts?|consensus|views?)"
# "target" alone is usually an analyst's price target; company targets name what they measure.
_GUIDE = r"(?:guidance|outlook|forecasts?|(?:revenue|sales|profit|earnings|margin|growth|financial)\s+targets?|view)"

POSITIVE_PATTERNS: dict[str, list[str]] = {
    "earnings_beat": [
        # "tops" / "topped" only: "Top Analyst Forecasts" is an adjective, not a beat.
        rf"\b(?:beats?|beat|tops|topped|exceeds?|exceeded|surpass(?:es|ed)?)\b(?:\s+\w+){{0,4}}\s+{_ESTIMATES}",
        rf"\babove\s+(?:\w+\s+){{0,2}}{_ESTIMATES}",
        r"\brecord\s+(?:quarterly\s+|annual\s+)?(?:profit|revenue|earnings|sales|bookings)\b",
        r"\b(?:beats?|tops)\s+(?:on\s+)?(?:earnings|profit|revenue|eps|sales)\b",
        r"^\S+(?:\s+\S+){0,3}\s+beats\b",
    ],
    "guidance_raise": [
        rf"\b(?:raises?|raised|lifts?|boosts?|ups|hikes?|increases?)\s+(?:its\s+)?(?:\S+\s+){{0,3}}{_GUIDE}\b",
    ],
    "fda_approval": [
        r"\bfda\s+(?:approves?|approved|grants?\s+(?:full\s+|accelerated\s+)?approval|clears?)\b",
        r"\b(?:receives?|wins?|gets?|secures?)\s+(?:\w+\s+){0,2}fda\s+(?:approval|clearance)\b",
    ],
    "positive_trial": [
        r"\b(?:meets?|met|achieves?|achieved|hits?|hit)\s+(?:its\s+|the\s+)?(?:\w+\s+)?(?:primary|main)\s+endpoints?\b",
        r"\bpositive\s+(?:topline|top-line|phase\s*\w+|pivotal|late-stage|trial|study)\b",
        r"\bstatistically\s+significant\b",
    ],
    # Deal wording only. A bare "buy" ("Buy signal", "stocks to buy", "upgraded to Buy") is never M&A.
    "m_and_a": [
        r"\b(?:to\s+)?(?:acquire|acquires)\b",
        r"\b(?:agrees?|agreed)\s+to\s+(?:acquire|buy|merge)\b",
        r"\btakeover\s+(?:bid|offer)\b",
        r"\b(?:to\s+be|agrees?\s+to\s+be)\s+(?:acquired|bought|taken\s+private)\b",
        r"\breceives?\s+(?:a\s+)?(?:buyout|takeover)\s+(?:bid|offer)\b",
    ],
    # Analyst actions only: "Upgrade Southern's Nuclear Plants" or "Power Upgrade Agreement" are not ratings.
    "upgrade": [
        r"\bupgraded\s+(?:to|by|at)\b|^[^,:;]{1,40}\bupgraded\b(?:\s*[,:;.]|$)",
        r"\bupgrades\s+(?:[\w.&'’-]+\s+){1,4}to\s+(?:buy|strong\s+buy|outperform|overweight|positive|accumulate|add|"
        r"market\s+outperform|sector\s+outperform)\b",
        r"\b(?:wins?|gets?|receives?|earns?|scores?)\s+(?:another\s+|an?\s+)?(?:analyst\s+)?upgrade\b",
        r"\b(?:analysts?|ratings?)\s+upgrades?\b|\bupgrades?\s+to\s+['‘’\"]?(?:buy|outperform|overweight)\b",
        r"\braises?\s+(?:its\s+)?price\s+target\b",
        r"\b(?:lifts?|boosts?|hikes?|raises?|ups)\s+(?:its\s+)?(?:price\s+)?target\s+(?:to|on)\s",
        r"\bprice[- ]target\s+(?:hike|raise|increase)\b",
        r"\b(?:initiated|initiates)\s+(?:at|with)\s+(?:buy|outperform|overweight)\b",
    ],
    "contract_win": [
        r"\b(?:wins?|won|awarded|secures?|secured|lands?|landed)\b(?:\s+\S+){0,5}\s+(?:contract|order|award|deal)s?\b",
        r"\b(?:supply|licensing|partnership|multi-year)\s+agreement\b",
    ],
    "insider_buy_cluster": [
        r"\b(?:insiders?|directors?|ceo|cfo|executives?|chairman)\s+(?:buy|buys|bought|purchase|purchases|purchased)\b",
    ],
}

NEGATIVE_CUES: dict[str, str] = {
    "guidance_cut": rf"\b(?:cuts?|lowers?|lowered|reduces?|slashes?|trims?|withdraws?|withdrawn|suspends?|pulls?)"
                    rf"\s+(?:its\s+)?(?:\S+\s+){{0,3}}{_GUIDE}\b|\b(?:weak|soft|disappointing)\s+{_GUIDE}\b",
    "earnings_miss": rf"\bmiss(?:es|ed)?\b|\bbelow\s+(?:\w+\s+){{0,2}}{_ESTIMATES}|\bfalls?\s+short\b"
                     r"|\bswings?\s+to\s+(?:a\s+)?(?:quarterly\s+)?loss\b|\bweak\s+(?:quarterly\s+)?results\b",
    "fda_negative": r"\bcomplete\s+response\s+letter\b|\bcrl\b|\bclinical\s+hold\b|\brefus(?:e|al)\s+to\s+file\b"
                    r"|\b(?:rejects?|rejected)\b",
    "label_restriction": r"\bboxed\s+warning\b|\bnarrower\s+label\b|\brestricted\s+label\b|\blimited\s+label\b",
    "trial_failure": r"\bfail(?:s|ed)?\s+to\s+meet\b|\bdid\s+not\s+meet\b|\bmiss(?:es|ed)?\s+(?:its\s+|the\s+)?"
                     r"(?:primary\s+)?endpoint\b|\bdiscontinu",
    "downgrade": r"\bdowngraded?\b|\bdowngrades\b",
    "target_cut": r"\bprice\s+target\s+(?:lowered|cut|reduced)\b|\b(?:lowers?|cuts?)\s+(?:its\s+)?price\s+target\b",
    "price_drop": r"\bshares?\s+(?:fall|falls|fell|slip|slips|slid|drop|drops|dropped|plunge|plunges|sink|sinks|"
                  r"tumble|tumbles|slump|slumps)\b|\bstock\s+(?:falls|drops|plunges|sinks|slides)\b",
    "margin_pressure": r"\bmargins?\s+(?:shrink|shrinks|narrow|narrows|contract|contracts|squeezed)\b",
    "legal_or_regulatory": r"\blawsuit\b|\bprobe\b|\binvestigation\b|\bsubpoena\b|\bfraud\b|\brecall(?:s|ed)?\b",
    "financing": r"\b(?:dilutive\s+)?(?:stock|share|equity)\s+offering\b|\bbankruptcy\b",
    "operations": r"\blayoffs?\b|\bhalts?\b|\bdelay(?:s|ed)?\b|\bresigns?\b|\bdeparture\b|\bsteps\s+down\b",
}

CONTRAST = re.compile(r"\b(?:but|while|though|although|despite|yet|however)\b|;", re.I)
ACQ_ACTIVE = re.compile(r"\b(?:to\s+)?(?:acquire|acquires)\b|\bagrees?\s+to\s+(?:acquire|buy|merge\s+with)\b"
                        r"|\btakeover\s+of\b", re.I)
ACQ_PASSIVE = re.compile(r"\b(?:to\s+be|agrees?\s+to\s+be)\s+(?:acquired|bought|taken\s+private)\b|\btakeover\s+(?:bid|"
                         r"offer)\b|\breceives?\s+(?:a\s+)?(?:buyout|takeover)\b", re.I)
MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(billion|bn|million|mln|m|b)\b", re.I)
PREMIUM = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*premium", re.I)
# Strong negative model score on a rule-positive headline means "mixed".
# Price-target-only actions (rating unchanged) are weaker news than a rating upgrade.
PRICE_TARGET_ONLY = re.compile(
    r"\braises?\s+(?:its\s+)?price\s+target\b|\b(?:lifts?|boosts?|hikes?|raises?|ups)\s+(?:its\s+)?(?:price\s+)?"
    r"target\s+(?:to|on)\s|\bprice[- ]target\s+(?:hike|raise|increase)\b", re.I)
RATING_UPGRADE = re.compile("|".join(POSITIVE_PATTERNS_UPGRADE_RATING := [
    r"\bupgraded\s+(?:to|by|at)\b|^[^,:;]{1,40}\bupgraded\b(?:\s*[,:;.]|$)",
    r"\bupgrades\s+(?:[\w.&'’-]+\s+){1,4}to\s+(?:buy|strong\s+buy|outperform|overweight|positive|accumulate|add|"
    r"market\s+outperform|sector\s+outperform)\b",
    r"\b(?:wins?|gets?|receives?|earns?|scores?)\s+(?:another\s+|an?\s+)?(?:analyst\s+)?upgrade\b",
    r"\b(?:analysts?|ratings?)\s+upgrades?\b|\bupgrades?\s+to\s+['‘’\"]?(?:buy|outperform|overweight)\b",
    r"\b(?:initiated|initiates)\s+(?:at|with)\s+(?:buy|outperform|overweight)\b",
]), re.I)

MODEL_VETO_NEG = 0.60
# Analyst upgrades are identified by explicit rating-action wording; on the first live
# FinBERT run the model scored two genuine Meta upgrades as strongly negative, so the
# veto is not applied to them. Revisit once outcome data can measure the veto.
MODEL_VETO_EXEMPT = frozenset({"upgrade"})


@dataclass(frozen=True)
class EventCandidate:
    symbol: str
    event_type: str
    polarity: str               # positive | neutral | negative | mixed
    strength: str | None        # strong | normal | weak (positive only)
    materiality: float
    reasons: tuple[str, ...]
    mixed_resolution: dict | None
    sentiment: dict | None
    classifier_version: str = CLASSIFIER_VERSION

    @property
    def is_signal_eligible(self) -> bool:
        return self.polarity == "positive" and self.event_type != "other"


@dataclass
class _Scan:
    positives: dict[str, list[re.Match]] = field(default_factory=dict)
    negatives: dict[str, list[re.Match]] = field(default_factory=dict)


def _scan(headline: str) -> _Scan:
    s = _Scan()
    for kind, patterns in POSITIVE_PATTERNS.items():
        hits = [m for p in patterns for m in re.finditer(p, headline, re.I)]
        if hits:
            s.positives[kind] = hits
    for kind, pattern in NEGATIVE_CUES.items():
        hits = list(re.finditer(pattern, headline, re.I))
        if hits:
            s.negatives[kind] = hits
    # "fails to meet / misses primary endpoint" is a trial failure: not a positive trial, not an earnings miss.
    if "trial_failure" in s.negatives:
        s.positives.pop("positive_trial", None)
        s.negatives.pop("earnings_miss", None)
    # A price-target raise is an upgrade cue; a price-target cut is not also a guidance cut.
    if "target_cut" in s.negatives:
        s.negatives.pop("guidance_cut", None)
    # "guidance raise" regex also matches "raises price target"; keep that as upgrade only.
    if "guidance_raise" in s.positives and all("price target" in m.group(0).lower()
                                                 for m in s.positives["guidance_raise"]):
        del s.positives["guidance_raise"]
    # An upgrade verb inside "downgraded" is not an upgrade.
    if "downgrade" in s.negatives and "upgrade" in s.positives and not re.search(r"(?<!down)upgrad", headline, re.I):
        del s.positives["upgrade"]
    return s


def has_catalyst_phrase(headline: str) -> bool:
    """True if any positive or negative catalyst phrase appears (used by the non-event filter)."""
    scan = _scan(headline)
    return bool(scan.positives) or bool(set(scan.negatives) - {"price_drop"})


def materiality(headline: str, market_cap: float | None = None) -> float:
    """0..1. Deal/contract size relative to market cap when known, else absolute tiers; deal premium."""
    score = 0.6
    amounts = []
    for num, unit in MONEY.findall(headline):
        mult = 1e9 if unit.lower() in ("billion", "bn", "b") else 1e6
        amounts.append(float(num) * mult)
    if amounts:
        biggest = max(amounts)
        if market_cap:
            ratio = biggest / market_cap
            score = 0.95 if ratio >= 0.10 else 0.8 if ratio >= 0.03 else 0.6 if ratio >= 0.01 else 0.35
        else:
            score = 0.9 if biggest >= 1e9 else 0.7 if biggest >= 1e8 else 0.45
    if (m := PREMIUM.search(headline)) and float(m.group(1)) >= 20:
        score = max(score, 0.9)
    return score


def _acquisition_roles(headline: str, mentions: Sequence[Mention]) -> dict[str, str]:
    """symbol -> 'target' | 'acquirer' for M&A headlines, based on position relative to the verb."""
    roles: dict[str, str] = {}
    spans = [m for m in mentions if m.start >= 0]
    if (p := ACQ_PASSIVE.search(headline)) is not None:
        for m in spans:
            roles[m.symbol] = "target" if m.start < p.start() else "acquirer"
        return roles
    if (a := ACQ_ACTIVE.search(headline)) is not None:
        for m in spans:
            roles[m.symbol] = "acquirer" if m.start < a.start() else "target"
    return roles


# Listed brokers whose analysts rate other companies. In "Stifel Upgrades Microsoft" the
# upgrade belongs to Microsoft, not Stifel.
BROKER_SYMBOLS = frozenset({
    "SF", "GS", "MS", "JPM", "JEF", "RJF", "PIPR", "EVR", "WFC", "C", "BAC", "BCS", "UBS", "DB", "TFC", "OPY", "KEY",
    "HSBC", "BMO", "RY", "TD", "CM", "BNS", "NMR", "LAZ", "SNEX", "MFG", "MUFG", "CS",
})
ANALYST_KINDS = frozenset({"upgrade"})


def _catalyst_subject(scan: _Scan, mentions: Sequence[Mention]) -> str | None:
    """The company a (non-M&A) catalyst describes: the mention closest before the first
    catalyst phrase ("Apple supplier Palantir wins ..." -> Palantir), else the first one
    after it ("FDA approves Madrigal ..." -> Madrigal), else the primary mention.
    For analyst actions, a broker is never the subject when another company is named."""
    spans = [m for m in mentions if m.start >= 0]
    if set(scan.positives) & ANALYST_KINDS:
        rated = [m for m in spans if m.symbol not in BROKER_SYMBOLS]
        if rated and len(rated) < len(spans):
            spans = rated
            mentions = [m for m in mentions if m.symbol not in BROKER_SYMBOLS]
    starts = [hit.start() for k, hits in scan.positives.items() if k != "m_and_a" for hit in hits]
    if not spans or not starts:
        return next((m.symbol for m in mentions if m.is_primary), None)
    anchor = min(starts)
    before = [m for m in spans if m.start < anchor]
    if before:
        return max(before, key=lambda m: m.start).symbol
    return min(spans, key=lambda m: m.start).symbol


def classify(headline: str, mentions: Sequence[Mention], sentiment: SentimentScore | None = None,
             market_caps: dict[str, float] | None = None) -> list[EventCandidate]:
    scan = _scan(headline)
    senti = sentiment.as_dict() if sentiment else None
    positive_kinds = [k for k in PRIORITY if k in scan.positives]
    # M&A needs an identifiable target: "upgraded to Buy" or "time to buy chip stocks" are not deals.
    roles = _acquisition_roles(headline, mentions) if "m_and_a" in scan.positives else {}
    has_ma = "target" in roles.values()
    if not has_ma:
        roles = {}
    subject = _catalyst_subject(scan, mentions)
    out: list[EventCandidate] = []

    for mention in mentions:
        sym = mention.symbol
        reasons: list[str] = []
        kinds = list(positive_kinds)
        if has_ma:
            role = roles.get(sym)
            if role == "target":
                kinds.insert(0, "m_and_a_target")
                reasons.append("acquisition target")
            elif role == "acquirer":
                reasons.append("acquirer side of a deal: not a positive catalyst for the buyer")
        if sym != subject and roles.get(sym) != "target":
            # Non-M&A catalysts describe the headline's subject, not every company mentioned.
            if kinds:
                reasons.append(f"catalyst belongs to {subject}")
            kinds = []
        if roles.get(sym) == "acquirer":
            kinds = [k for k in kinds if k != "m_and_a_target"]
        kinds = [k for k in PRIORITY if k in kinds]
        negatives = sorted(scan.negatives)
        mixed: dict | None = None

        if kinds:
            event_type = kinds[0]
            polarity, strength = "positive", ("strong" if len(kinds) >= 2 else "normal")
            if event_type == "upgrade" and not RATING_UPGRADE.search(headline):
                strength = "weak"
                reasons.append("price-target raise without a rating change")
            reasons.append("catalyst: " + ", ".join(kinds))
            conflict = None
            if "guidance_cut" in scan.negatives:
                conflict = "guidance cut/withdrawn overrides the positive catalyst"
            elif event_type == "fda_approval" and "label_restriction" in scan.negatives:
                conflict = "approval comes with a boxed warning or narrower label"
            elif event_type == "upgrade" and "target_cut" in scan.negatives:
                conflict = "upgrade paired with a lowered price target"
            elif "earnings_beat" in kinds and "earnings_miss" in scan.negatives:
                conflict = "beat on one measure, miss on another"
            elif negatives:
                conflict = "negative cue alongside the catalyst: " + ", ".join(negatives)
            if conflict:
                polarity, strength = "mixed", None
                mixed = {"conflict": conflict, "positive": kinds, "negative": negatives,
                         "clauses": [c.strip() for c in CONTRAST.split(headline) if c and c.strip()]}
            elif (sentiment is not None and sentiment.negative >= MODEL_VETO_NEG
                  and event_type not in MODEL_VETO_EXEMPT):
                polarity, strength = "mixed", None
                mixed = {"conflict": f"sentiment model strongly negative ({sentiment.negative:.2f})",
                         "positive": kinds, "negative": ["model"]}
            elif sentiment is not None and sentiment.margin >= 0.2:
                reasons.append(f"sentiment agrees ({sentiment.model}, margin {sentiment.margin:+.2f})")
        else:
            event_type, strength = "other", None
            if negatives and sym == subject:
                polarity = "negative"
                reasons.append("negative cue: " + ", ".join(negatives))
            elif sentiment is not None and sym == subject:
                polarity = sentiment.label
                reasons.append(f"no catalyst; sentiment {sentiment.label} ({sentiment.model})")
            else:
                polarity = "neutral"
                if not reasons:
                    reasons.append("no catalyst")

        mat = materiality(headline, (market_caps or {}).get(sym)) if event_type != "other" else 0.0
        out.append(EventCandidate(sym, event_type, polarity, strength, mat, tuple(reasons), mixed, senti))
    return out
