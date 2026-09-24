"""Ticker linking: which listed companies is a headline about?

Methods, in order of trust (confidence in brackets):
  exchange tag   "(NYSE: ALL)", "(NASDAQ:NVDA)"                     [0.97]
  cashtag        "$NVDA"                                             [0.95]
  provider tag   provider's own ticker list, scaled by its score     [0.60 + 0.35 x score]
  company name   aliases built from SEC's company list               [0.90 multi-word, 0.85 single word]
  bare ticker    all-caps "AMD" in the headline (3+ letters)         [0.80]

A symbol found by two or more methods gets +0.05 (max 0.99). Only links with
confidence >= 0.80 are kept. Tickers that are ordinary English words (ALL,
NOW, IT, ON, ...) are never linked from a bare word or a provider tag alone;
they need an exchange tag, a cashtag, or a company-name match. Every rejected
or ambiguous candidate is written to the ambiguity log with a reason.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

LINK_THRESHOLD = 0.80

# Tickers that are also everyday words or common acronyms in headlines.
COMMON_WORD_TICKERS = frozenset({
    "A", "ALL", "AN", "ANY", "ARE", "BE", "BIG", "BEST", "CAN", "CAR", "CASH", "COST", "DAY", "EAT", "FOR",
    "FUN", "GO", "GOOD", "HAS", "HE", "HOPE", "IT", "KEY", "LOVE", "LOW", "MAN", "NEW", "NEXT", "NICE",
    "NOW", "ON", "ONE", "OPEN", "OR", "OUT", "PEAK", "PLAY", "REAL", "RUN", "SAFE", "SEE", "SO", "TRUE",
    "TWO", "UP", "US", "WELL", "WIN", "YOU"
})
NOT_TICKERS = frozenset({
    "CEO", "CFO", "COO", "CTO", "FDA", "SEC", "EPS", "IPO", "ETF", "USA", "GDP", "CPI", "FED", "AI", "EV",
    "ESG", "IRS", "NYSE", "LLC", "INC", "ADR", "EU", "UK", "OTC", "Q1", "Q2", "Q3", "Q4", "US", "PMI",
    "API", "DOJ", "FTC", "CRL", "NDA", "BLA", "PDUFA", "IT"
})
NAME_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|companies|holdings?|group|ltd|limited|plc|sa|nv|ag|"
    r"lp|llc|trust|the)\b\.?",
    re.I,
)
# First words too generic to stand alone as an alias.
GENERIC_FIRST_WORDS = frozenset({
    "first", "american", "general", "united", "national", "international", "advanced", "global", "new",
    "great", "southern", "northern", "western", "eastern", "capital", "digital", "energy", "health",
    "medical", "bank", "financial", "royal", "standard", "applied", "universal", "public", "premier",
    "allied", "pacific", "atlantic", "central"
})
# Well-known names SEC's titles don't contain.
EXTRA_ALIASES: Mapping[str, tuple[str, ...]] = {
    "GOOGL": ("Google",), "META": ("Facebook",), "AMD": ("AMD",), "IBM": ("IBM",), "HPQ": ("HP",),
}

EXCHANGE_TAG = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE(?:\s+(?:American|Arca))?|AMEX|Cboe|CBOE)\s*:\s*([A-Z][A-Z.]{0,5})\)"
)
CASHTAG = re.compile(r"(?<![A-Za-z0-9])\$([A-Z]{1,5})(?![A-Za-z0-9])")
BARE_TICKER = re.compile(r"(?<![A-Za-z0-9$])([A-Z]{3,5})(?![A-Za-z0-9])")


@dataclass(frozen=True)
class Company:
    symbol: str
    cik: str | None
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Mention:
    symbol: str
    method: str
    confidence: float
    start: int = -1          # span in the headline; -1 for provider tags without a span
    end: int = -1
    is_primary: bool = False


@dataclass(frozen=True)
class Ambiguity:
    candidates: tuple[tuple[str, str, float], ...]   # (symbol, method, confidence)
    chosen: str | None
    reason: str


@dataclass
class LinkResult:
    mentions: list[Mention] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.mentions]

    @property
    def primary(self) -> str | None:
        return next((m.symbol for m in self.mentions if m.is_primary), None)


def clean_name(title: str) -> str:
    """'VERTEX PHARMACEUTICALS INC / MA' -> 'Vertex Pharmaceuticals'."""
    name = title.split("/")[0]
    name = NAME_SUFFIXES.sub(" ", name.replace(",", " ").replace("&", " "))
    # SEC titles are often ALL CAPS: "NVIDIA CORP" -> "Nvidia". Short all-caps words ("ON", "AMD") stay as-is.
    return " ".join(w.capitalize() if w.isupper() and len(w) > 3 else w for w in name.split())


def make_aliases(symbol: str, title: str) -> tuple[str, ...]:
    base = clean_name(title)
    aliases = {base} if base else set()
    words = base.split()
    if len(words) > 1 and words[0].lower() not in GENERIC_FIRST_WORDS and len(words[0]) >= 4:
        aliases.add(words[0])
    aliases.update(EXTRA_ALIASES.get(symbol, ()))
    return tuple(sorted(aliases, key=len, reverse=True))


class Universe:
    """Listed US companies. One canonical symbol per company (CIK); other share
    classes map to it."""

    def __init__(self, companies: Iterable[Company], share_classes: Mapping[str, str] | None = None):
        self.by_symbol: dict[str, Company] = {c.symbol: c for c in companies}
        self.canonical: dict[str, str] = {s: s for s in self.by_symbol}
        self.canonical.update(share_classes or {})
        alias_map: dict[str, set[str]] = defaultdict(set)
        for c in self.by_symbol.values():
            for a in c.aliases:
                alias_map[a.lower()].add(c.symbol)
        # Longest aliases first so "ON Semiconductor" wins over a shorter overlap.
        self._aliases = sorted(alias_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        self._alias_patterns = {
            a: re.compile(rf"(?<![A-Za-z0-9]){re.escape(a)}(?:'s|’s)?(?![A-Za-z0-9])", re.I)
            for a, _ in self._aliases
        }

    @classmethod
    def from_sec_company_tickers(cls, data: Mapping[str, Mapping]) -> Universe:
        """Build from SEC's company_tickers.json. The first symbol listed for a
        CIK is treated as canonical (SEC lists the primary class first)."""
        companies: list[Company] = []
        share_classes: dict[str, str] = {}
        first_by_cik: dict[str, str] = {}
        for _, row in sorted(data.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            symbol = str(row["ticker"]).upper().replace("-", ".")
            cik = str(row["cik_str"]).zfill(10)
            if cik in first_by_cik:
                share_classes[symbol] = first_by_cik[cik]
                continue
            first_by_cik[cik] = symbol
            companies.append(Company(symbol, cik, clean_name(row["title"]), make_aliases(symbol, row["title"])))
        return cls(companies, share_classes)

    def resolve(self, symbol: str) -> str | None:
        return self.canonical.get(symbol.upper().replace("-", "."))

    def alias_matches(self, headline: str) -> list[tuple[str, int, int, str]]:
        """(symbol, start, end, alias) for company-name matches, longest first, non-overlapping.
        Requires the matched text to start with a capital letter ("apple" the fruit is not Apple)."""
        taken: list[tuple[int, int]] = []
        out = []
        for alias, symbols in self._aliases:
            for m in self._alias_patterns[alias].finditer(headline):
                if not headline[m.start()].isupper():
                    continue
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                for sym in symbols:
                    out.append((sym, m.start(), m.end(), alias))
        return out


def link_tickers(headline: str, provider_tickers: Mapping[str, float], universe: Universe) -> LinkResult:
    found: dict[str, list[Mention]] = defaultdict(list)
    result = LinkResult()

    def add(raw_symbol: str, method: str, conf: float, start: int = -1, end: int = -1) -> None:
        symbol = universe.resolve(raw_symbol)
        if symbol is None:
            result.ambiguities.append(Ambiguity(((raw_symbol, method, conf),), None, "unknown_ticker"))
            return
        if symbol != raw_symbol.upper():
            result.ambiguities.append(Ambiguity(((raw_symbol, method, conf),), symbol, "share_class_collapsed"))
        found[symbol].append(Mention(symbol, method, conf, start, end))

    for m in EXCHANGE_TAG.finditer(headline):
        add(m.group(1), "exchange_tag", 0.97, m.start(1), m.end(1))
    for m in CASHTAG.finditer(headline):
        add(m.group(1), "cashtag", 0.95, m.start(1), m.end(1))
    for sym, score in provider_tickers.items():
        add(sym, "provider_tag", round(0.60 + 0.35 * max(0.0, min(1.0, score)), 3))

    alias_hits = universe.alias_matches(headline)
    by_span: dict[tuple[int, int], list[tuple[str, str]]] = defaultdict(list)
    for sym, s, e, alias in alias_hits:
        by_span[(s, e)].append((sym, alias))
    for (s, e), hits in by_span.items():
        if len(hits) > 1:
            result.ambiguities.append(Ambiguity(tuple((sym, "name", 0.0) for sym, _ in hits), None,
                                                "multiple_companies_same_alias"))
            continue
        sym, alias = hits[0]
        add(sym, "name", 0.90 if " " in alias else 0.85, s, e)

    for m in BARE_TICKER.finditer(headline):
        word = m.group(1)
        if word in NOT_TICKERS or word in COMMON_WORD_TICKERS or word not in universe.by_symbol:
            continue
        if any(x.start <= m.start() < x.end for ms in found.values() for x in ms if x.start >= 0):
            continue   # already part of a tag or name match
        add(word, "bare_ticker", 0.80, m.start(1), m.end(1))

    accepted: list[Mention] = []
    for symbol, mentions in found.items():
        methods = {m.method for m in mentions}
        best = max(mentions, key=lambda m: m.confidence)
        conf = best.confidence + (0.05 if len(methods) >= 2 else 0.0)
        conf = min(0.99, conf)
        if symbol in COMMON_WORD_TICKERS and not methods & {"exchange_tag", "cashtag", "name"}:
            result.ambiguities.append(Ambiguity(tuple((m.symbol, m.method, m.confidence) for m in mentions), None,
                                                "common_word_ticker_needs_tag_or_name"))
            continue
        if conf < LINK_THRESHOLD:
            result.ambiguities.append(Ambiguity(tuple((m.symbol, m.method, m.confidence) for m in mentions), None,
                                                "below_threshold"))
            continue
        spans = [m for m in mentions if m.start >= 0]
        first = min(spans, key=lambda m: m.start) if spans else best
        accepted.append(Mention(symbol, "+".join(sorted(methods)), round(conf, 3), first.start, first.end))

    accepted.sort(key=lambda m: (m.start < 0, m.start, -m.confidence))
    if accepted:
        accepted[0] = dataclasses.replace(accepted[0], is_primary=True)
    result.mentions = accepted
    return result
