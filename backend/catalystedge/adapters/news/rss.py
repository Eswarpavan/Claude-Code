"""Official RSS/Atom feeds (newswires, agencies). Primary sources for catalyst confirmation.

Only what the feed publishes for syndication is read: title, link, publication time and the exchange
ticker tags ("(NASDAQ: ABC)") that newswires put in their items. The item description is scanned for
those tags in memory and never stored (RawNews has no body field by design).
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from catalystedge.adapters.news.base import NewsAdapter, RawNews

TICKER_TAG = re.compile(r"\((?:NASDAQ|NYSE(?:\s+American|\s+MKT|\s+Arca)?|Nasdaq(?:GS|GM|CM)?|NYSEAMERICAN|AMEX|"
                        r"CBOE|OTCQX|OTCQB)\s*[:\-]\s*([A-Z]{1,5}(?:\.[A-Z])?)\)", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: dt.datetime | None
    guid: str
    tickers: tuple[str, ...]


def _date(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)             # RSS 2.0: RFC 822
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))   # Atom: RFC 3339
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def _text(el: ET.Element | None) -> str:
    return html.unescape("".join(el.itertext())).strip() if el is not None else ""


def parse_feed(xml_text: str) -> list[FeedItem]:
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out: list[FeedItem] = []
    for it in root.iter("item"):                                   # RSS 2.0
        title, link = _text(it.find("title")), _text(it.find("link"))
        extra = " ".join(_text(c) for c in it.findall("category")) + " " + _text(it.find("description"))
        out.append(FeedItem(title, link, _date(_text(it.find("pubDate")) or None),
                            _text(it.find("guid")) or link, _tickers(title + " " + extra)))
    for it in root.iter(f"{ATOM}entry"):                           # Atom
        link_el = it.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        title = _text(it.find(f"{ATOM}title"))
        extra = " ".join(c.get("term", "") for c in it.findall(f"{ATOM}category")) + " " + \
            _text(it.find(f"{ATOM}summary"))
        pub = _text(it.find(f"{ATOM}published")) or _text(it.find(f"{ATOM}updated"))
        out.append(FeedItem(title, link, _date(pub), _text(it.find(f"{ATOM}id")) or link,
                            _tickers(title + " " + extra)))
    return [i for i in out if i.title and i.link and i.published]


def ensure_feed(text: str, source: str) -> str:
    """Refuse a web page served where a feed was expected (found live: an error page parsed as 'no items')."""
    head = text[:2000].lstrip("\ufeff \r\n\t").lower()
    if "<rss" not in head and "<feed" not in head and "<rdf:rdf" not in head:
        from catalystedge.core.http import ProviderError

        raise ProviderError(source, "returned a web page, not an RSS/Atom feed (the feed URL may have changed)")
    return text


def _tickers(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.upper() for m in TICKER_TAG.findall(text)))


class RssNews(NewsAdapter):
    """One official feed. `source_key` must exist in sources.SOURCES (its limits and credibility)."""

    def __init__(self, http, source_key: str, url: str, publisher: str, enabled: bool = True):
        super().__init__(http)
        self.source_key, self.url, self.publisher = source_key, url, publisher
        self.disabled_reason = None if enabled else "switched off in settings"

    def _fetch(self, *, since: dt.datetime, now: dt.datetime, symbols: list[str]) -> list[RawNews]:
        text = ensure_feed(self.http.get_text(self.source_key, self.url,
                                              headers={"Accept": "application/rss+xml, application/xml"}),
                           self.source_key)
        out = []
        for i in parse_feed(text):
            if since <= i.published <= now:
                out.append(RawNews(self.source_key, i.guid[:200], i.title, i.link, i.published, self.publisher,
                                   provider_tickers={t: 0.9 for t in i.tickers}))
        return out
