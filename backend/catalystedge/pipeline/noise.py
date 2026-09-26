"""Non-event filter: drop headlines that carry no new company event.

Runs after dedupe and before classification. A filtered headline is still
stored as news, but it never produces an event, so it can never become a
signal. Filters, in the order they are checked:

  law_firm_solicitation   "XYZ Investors Have Opportunity to Lead ... Class Action", "Rosen Law Firm Encourages ..."
  listicle                "2 Monster Dividend Stocks to ...", "Top 5 ...", "3 Reasons ..."
  stock_picking_advice    "stocks to buy", "Is X a Buy?", "I'd still buy", "32% overpriced", "buy signal"
  long_range_speculation  "Where Will X Be in 5 Years?", "Prediction: ...", "could be worth ... in a decade"
  price_move_only         "surge to 52-week highs", "Why X Stock Jumped Today", "Dips More Than Broader Market"
  market_commentary       index / futures / "stock market" wrap-ups, "What You Should Know" templates
  opinion_or_question     "Is an AMD Stock Split Coming?", "How to Play MCD Stock", "Take Profits, or Buy More?"

The last three only apply when the classifier finds no catalyst phrase in the
headline, so "Nvidia beats estimates; Nasdaq futures rise" is kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from catalystedge.pipeline.classify import has_catalyst_phrase

FILTER_VERSION = "noise-v2"

_NUM = r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty)"

_LAW_FIRMS = (r"rosen\s+law|pomerantz|robbins\s+(?:llp|geller)|levi\s*&\s*korsinsky|kahn\s+swick|bragar\s+eagel|"
              r"glancy\s+prongay|faruqi|bernstein\s+liebhard|schall\s+law|gross\s+law|portnoy\s+law|kessler\s+topaz|"
              r"hagens\s+berman|bronstein,?\s+gewirtz|johnson\s+fistel|block\s*&\s*leviton|rigrodsky|halper\s+sadeh|"
              r"ademi|monteverde|wolf\s+haldenstein|brodsky\s*&\s*smith|holzer\s*&\s*holzer|lowey\s+dannenberg|"
              r"howard\s+g\.?\s+smith|frank\s+r\.?\s+cruz|thornton\s+law|kirby\s+mcinerney|scott\+scott")

ALWAYS: dict[str, list[str]] = {
    # Shareholder-lawsuit advertising by law firms (found live on the newswires and aggregators): recruiting
    # plaintiffs about an old stock drop is not a new company event. Real legal news ("settles class action for
    # $50 million", "court dismisses suit") does not match.
    "law_firm_solicitation": [
        rf"\b(?:{_LAW_FIRMS})\b",
        r"\b(?:investors?|shareholders?|stockholders?)\s+(?:have|has)\s+(?:an\s+)?opportunity\s+to\s+lead\b",
        r"\b(?:application|lead[- ]plaintiff)\s+deadline\b|\bdeadline\s+(?:reminder|alert)\b",
        r"\b(?:investors?|shareholders?|stockholders?)\s+(?:who\s+(?:lost|purchased|bought|acquired)|with\s+losses|"
        r"are\s+(?:encouraged|reminded|invited))\b",
        r"\b(?:shareholder|stockholder|investor)\s+(?:alert|notice|news|rights\s+law\s+firm)\b",
        r"\binsiders\s+breach\s+their\s+fiduciary\s+duties\b",
        r"\binvestigation\s+(?:initiated|announced|on\s+behalf\s+of)\b|\binvestigates\s+the\s+officers\b",
        r"\bencourages?\s+.{0,80}\binvestors?\s+to\s+(?:inquire|contact|join)\b",
    ],
    "listicle": [
        rf"^{_NUM}\s+(?:[\w'’-]+\s+){{0,5}}(?:stocks?|shares|reasons?|things|ways|picks|companies|charts?|"
        r"etfs?|funds|names|winners|losers|lessons|mistakes|signs|moves|features)\b",
        rf"\b(?:top|best|worst)\s+{_NUM}\b|\bhere\s+are\s+(?:the\s+)?{_NUM}\b",
        rf"\b{_NUM}\s+(?:[\w'’-]+\s+){{0,3}}stocks?\s+(?:to|that|for|i'?m|you)\b",
    ],
    "stock_picking_advice": [
        r"\bstocks?\s+to\s+(?:buy|sell|own|hold|watch|avoid)\b",
        r"\b(?:is|are)\s+(?:it|now|this|[\w.&'’-]+(?:\s+[\w.&'’-]+){0,2})\s+(?:a|still\s+a)\s+(?:buy|sell|hold)\b",
        r"\b(?:buy|sell)\s+(?:now|today|signal|rating\s+reiterated)\b|\bbuy\s+signal\b",
        r"\b(?:should\s+you|time\s+to|worth)\s+(?:buy|buying|sell|selling|own|owning)\b",
        # A blog contributor's own rating, e.g. "... (Rating Upgrade)", is opinion, not a broker action.
        r"\((?:rating|ratings)\s+(?:upgrade|downgrade|reiteration|reiterated|maintained)\)\s*$",
        r"\b(?:i'?d|i\s+would|would\s+i)\s+(?:still\s+)?(?:buy|sell|own)\b|\bstill\s+buy\b",
        r"\b(?:overpriced|overvalued|undervalued|fully\s+priced|cheap\s+stock|bargain)\b",
        r"\bbuy\s+the\s+dip\b|\bmillionaire[- ]maker\b|\bno[- ]brainer\b|\bset\s+for\s+life\b",
        # "Only One Clear Buy Among NVDA, AVGO, MU, AMD", "the better buy", "top buy"
        r"\b(?:clear|better|best|top|only|smart|strong)\s+buys?\b|\bbuys?\s+among\b",
    ],
    "long_range_speculation": [
        r"\bwhere\s+will\b.*\bbe\s+in\s+\d+\s+years?\b",
        r"^prediction:",
        r"\b(?:in|over)\s+(?:a|the\s+next|\d+)\s+(?:decade|years)\b.*\?$|\bin\s+a\s+decade\b",
        r"\bcould\s+be\s+worth\b|\binvested\s+in\b.*\bwould\s+be\s+worth\b|\bif\s+you\s+invested\b",
    ],
}

WITHOUT_CATALYST: dict[str, list[str]] = {
    "price_move_only": [
        r"\b(?:52-week|all-time|record|multi-year|\d+-(?:month|year))\s+(?:highs?|lows?)\b",
        r"^why\s+.+\s+(?:stock|shares)\s+(?:jumped|soared|surged|rose|fell|dropped|sank|plunged|tumbled|dived|dove|"
        r"slumped|popped|rallied|slid)\b",
        r"^why\s+.+\s+(?:stock|shares)\s+(?:is|are|was|were)\s+(?:trading\s+)?(?:up|down|higher|lower|soaring|"
        r"sinking|falling|rising|sliding|jumping|climbing|dropping|a\s+winner|a\s+loser)\b",
        r"^why\s+did\s+.+\s+(?:surge|soar|jump|fall|drop|sink|plunge|rally)\b",
        r"^why\s+.+\s+(?:is|are)\s+(?:up|down)\s+\d",
        r"\b(?:is|are)\s+(?:up|down)\s+\d+(?:\.\d+)?%\s+(?:this|so\s+far|year|today|in\s+\d{4})",
        r"\b(?:stock|shares)\s+(?:is\s+)?(?:up|down|jumps?|surges?|soars?|falls?|drops?|slides?|sinks?|climbs?|"
        r"rises?|steadies|slips?|tumbles?|rallies|plunges?|pops?|hits?)\b",
        r"\bshares\s+are\s+(?:falling|rising|sliding|soaring|trading)\b",
        r"\b(?:worst|best)\s+(?:day|week|drop|month)\b",
        r"\b(?:dips|gains|rises|falls|climbs|slides|outpaces|lags|trails)\b.*\b(?:than|as)\s+(?:the\s+)?"
        r"(?:broader\s+)?market\b|\bbigger\s+(?:fall|gain|drop)\s+than\s+the\s+market\b",
    ],
    "market_commentary": [
        r"\b(?:dow(?:\s+jones)?|s&p\s*500|nasdaq(?:\s+composite)?|russell\s+2000|stock\s+market|wall\s+street|"
        r"futures|stocks)\s+(?:today|rise|rises|fall|falls|dip|dips|slip|slips|climb|climbs|rally|rallies|"
        r"edge|edges|slide|slides|gain|gains|drop|drops|surge|surges|tumble|tumbles|mixed|higher|lower|flat)\b",
        r"\bstock\s+market\s+today\b|\bmarket\s+(?:wrap|recap|outlook|update)\b|\bstocks\s+making\s+the\s+biggest\s+moves\b",
        r"\b(?:ai\s+bubble|recession|bear\s+market|bull\s+market|correction)\b",
        r"\bwhat\s+you\s+(?:should|need\s+to)\s+know\b|\bimportant\s+facts\s+to\s+note\b",
        r"\bstocks\s+that\s+explain\b|\bsector\s+update\b|\bhistory\s+of\b|\bcompany\s+timeline\b",
    ],
    "opinion_or_question": [
        r"\?\s*$",
        r"\bhow\s+to\s+play\b|\btake\s+profits\b|\bbuy\s+more\b|\b(?:buy|sell|own)\s+this\b",
        r"^(?:is|are|does|do|should|can|will|could|would|what|which|how|who)\b",
    ],
}

_ALWAYS = {k: [re.compile(p, re.I) for p in ps] for k, ps in ALWAYS.items()}
_WITHOUT = {k: [re.compile(p, re.I) for p in ps] for k, ps in WITHOUT_CATALYST.items()}
FILTERS = tuple(ALWAYS) + tuple(WITHOUT_CATALYST)


@dataclass(frozen=True)
class NoiseVerdict:
    reason: str        # one of FILTERS
    matched: str       # the text that triggered it


def non_event_reason(headline: str) -> NoiseVerdict | None:
    """The first filter the headline trips, or None if it may describe a company event."""
    text = headline.strip()
    for reason, patterns in _ALWAYS.items():
        for p in patterns:
            if m := p.search(text):
                return NoiseVerdict(reason, m.group(0))
    if has_catalyst_phrase(text):
        return None
    for reason, patterns in _WITHOUT.items():
        for p in patterns:
            if m := p.search(text):
                return NoiseVerdict(reason, m.group(0))
    return None
