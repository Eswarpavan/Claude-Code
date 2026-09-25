"""Collapse the same story reported by several providers into one cluster.

Two items are the same story if their normalised URLs match, or if their
headlines share at least 75% of their meaningful words (Jaccard). Headlines
are short, so this is more reliable than simhash distance on them; a 64-bit
simhash is still stored for the database index.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from catalystedge.adapters.news.base import RawNews

JACCARD_THRESHOLD = 0.75
STOPWORDS = frozenset({
    "a", "an", "and", "the", "of", "to", "for", "in", "on", "at", "by", "with", "as", "after",
    "before", "from", "its", "it's", "is", "are", "was", "were", "be", "into", "over", "than", "that",
    "this", "says", "said"
})
TOKEN = re.compile(r"[a-z0-9$%.]+")


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "ref", "src"))]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), path, urlencode(query), ""))


def tokens(headline: str) -> frozenset[str]:
    words = (w.strip(".") for w in TOKEN.findall(headline.lower()))
    return frozenset(w for w in words if w and w not in STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def simhash64(headline: str) -> int:
    """64-bit simhash as a signed integer (fits Postgres BIGINT)."""
    weights = [0] * 64
    for tok in tokens(headline):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    value = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


@dataclass
class Cluster:
    members: list[RawNews] = field(default_factory=list)

    @property
    def representative(self) -> RawNews:
        """The canonical report: the original newswire/agency release when one is in the cluster (earliest of
        those), otherwise the earliest report."""
        from catalystedge.sources import SOURCES

        primaries = [m for m in self.members if getattr(SOURCES.get(m.source_key), "primary", False)]
        return min(primaries or self.members, key=lambda m: (m.published_at, m.source_key))

    @property
    def is_primary(self) -> bool:
        from catalystedge.sources import SOURCES

        return getattr(SOURCES.get(self.representative.source_key), "primary", False)

    @property
    def provider_tickers(self) -> dict[str, float]:
        merged: dict[str, float] = {}
        for m in self.members:
            for t, s in m.provider_tickers.items():
                merged[t] = max(merged.get(t, 0.0), s)
        return merged

    @property
    def sources(self) -> list[str]:
        return sorted({m.source_key for m in self.members})


def cluster_items(items: list[RawNews]) -> list[Cluster]:
    clusters: list[Cluster] = []
    keys: list[tuple[set[str], frozenset[str]]] = []
    for item in sorted(items, key=lambda i: i.published_at):
        url, toks = normalize_url(item.url), tokens(item.headline)
        for cluster, (urls, ctoks) in zip(clusters, keys, strict=True):
            if url in urls or jaccard(toks, ctoks) >= JACCARD_THRESHOLD:
                cluster.members.append(item)
                urls.add(url)
                break
        else:
            clusters.append(Cluster([item]))
            keys.append(({url}, toks))
    return clusters
