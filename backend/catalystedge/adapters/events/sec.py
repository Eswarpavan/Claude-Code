"""SEC EDGAR: the "latest filings" Atom feed, filing indexes, 8-K press releases, Form 4 XML.

Official endpoints only, with the declared User-Agent SEC requires (name + email) and
the fair-access spacing in sources.py (<= ~6 requests/s). `accepted_at` is SEC's
acceptance time from the feed, which is when the filing became public (rule 7).
"""

from __future__ import annotations

import datetime as dt
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

from catalystedge.core.http import HttpClient

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_LINK = re.compile(r'<link[^>]*href="([^"]+)"')
_UPDATED = re.compile(r"<updated>(.*?)</updated>")
_SUMMARY = re.compile(r"<summary[^>]*>(.*?)</summary>", re.S)
_ACC = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
_TITLE_PARTS = re.compile(r"^(?P<form>[\w/-]+) - (?P<name>.*) \((?P<cik>\d{10})\) \((?P<role>\w+)\)$")
_ITEM = re.compile(r"Item (\d+\.\d+)")


@dataclass(frozen=True)
class FeedEntry:
    form: str
    company: str
    cik: str
    role: str                 # Filer | Issuer | Reporting
    accession: str
    accepted_at: dt.datetime
    items: tuple[str, ...]
    index_url: str

    @property
    def folder(self) -> str:
        return self.index_url.rsplit("/", 1)[0]


def parse_feed(xml_text: str) -> list[FeedEntry]:
    out = []
    for block in _ENTRY.findall(xml_text):
        t = _TITLE.search(block)
        title = html.unescape(t.group(1).strip()) if t else ""
        m = _TITLE_PARTS.match(title)
        acc = _ACC.search(block)
        link = _LINK.search(block)
        upd = _UPDATED.search(block)
        if not (m and acc and link and upd):
            continue
        summary = html.unescape(_SUMMARY.search(block).group(1)) if _SUMMARY.search(block) else ""
        try:
            accepted = dt.datetime.fromisoformat(upd.group(1)).astimezone(dt.UTC)
        except ValueError:
            continue
        out.append(FeedEntry(
            form=m["form"], company=m["name"], cik=m["cik"], role=m["role"], accession=acc.group(1),
            accepted_at=accepted,
            items=tuple(dict.fromkeys(_ITEM.findall(summary))), index_url=link.group(1),
        ))
    return out


def fetch_feed(http: HttpClient, ua: str, form: str, start: int = 0, count: int = 100) -> list[FeedEntry]:
    text = http.get_text("sec_feed", FEED_URL, {
        "action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include", "start": start,
        "count": count, "output": "atom"}, headers={"User-Agent": ua})
    return parse_feed(text)


# ----------------------------------------------------------------------------- filing documents


class _IndexTable(HTMLParser):
    """Rows of the "Document Format Files" table on a filing's -index.htm page."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.hrefs: list[list[str]] = []
        self._row: list[str] | None = None
        self._href: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row, self._href = [], []
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "a" and self._row is not None:
            href = dict(attrs).get("href")
            if href:
                self._href.append(href)

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self.hrefs.append(self._href)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def documents(index_html: str) -> list[tuple[str, str]]:
    """(document type, absolute URL) for each document in a filing index page."""
    p = _IndexTable()
    p.feed(index_html)
    out = []
    for row, hrefs in zip(p.rows, p.hrefs, strict=True):
        if len(row) >= 4 and hrefs:
            href = hrefs[0]
            if href.startswith("/ix?doc="):
                href = href[len("/ix?doc="):]
            href = href.split("?")[0]
            url = href if href.startswith("http") else "https://www.sec.gov" + href
            out.append((row[3], url))
    return out


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "li", "td", "title"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_BOILER = re.compile(r"^(?:exhibit\s*99|ex-?99|press\s+release|news\s+release|for\s+immediate\s+release|"
                     r"contact|media\s+contact|investor|source:|\(?nasdaq|\(?nyse|page\s+\d)", re.I)
_DATELINE = re.compile(r"^[A-Z][A-Za-z .,'-]+,\s+[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4}")
_MONTH_DATE = re.compile(r"^(?:january|february|march|april|may|june|july|august|september|october|november|"
                         r"december)\s+\d{1,2},\s+\d{4}$", re.I)


def headline_from_html(doc_html: str) -> str | None:
    """The press release headline: the first line of 6-45 words that is not boilerplate."""
    p = _Text()
    p.feed(doc_html)
    lines = [" ".join(x.split()).lstrip("•·▪●-–—* ").strip() for x in "".join(p.parts).split("\n")]
    for line in lines[:80]:
        words = line.split()
        if not 6 <= len(words) <= 45 or _BOILER.match(line) or _MONTH_DATE.match(line) or _DATELINE.match(line):
            continue
        return html.unescape(line)
    return None


def press_release_headline(http: HttpClient, ua: str, entry: FeedEntry) -> str | None:
    index_html = http.get_text("sec_edgar", entry.index_url, headers={"User-Agent": ua})
    docs = documents(index_html)
    ex99 = [u for t, u in docs if t.upper().startswith("EX-99")]
    if not ex99:
        return None
    return headline_from_html(http.get_text("sec_edgar", ex99[0], headers={"User-Agent": ua}))


# ----------------------------------------------------------------------------- Form 4


@dataclass(frozen=True)
class Form4Txn:
    symbol: str
    issuer_cik: str
    insider_name: str
    insider_role: str | None
    txn_code: str
    acquired_disposed: str
    shares: float
    price: float | None
    txn_date: dt.date
    is_10b5_1: bool

    @property
    def value_usd(self) -> float | None:
        return self.shares * self.price if self.price else None


@dataclass
class Form4:
    issuer_cik: str
    symbol: str
    transactions: list[Form4Txn] = field(default_factory=list)


def _t(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(xml_text: str) -> Form4 | None:
    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError:
        return None
    symbol = (_t(root, "issuer/issuerTradingSymbol") or "").upper().replace("-", ".")
    cik = (_t(root, "issuer/issuerCik") or "").zfill(10)
    if not symbol:
        return None
    plan = (_t(root, "aff10b5One") or "0").lower() in ("1", "true")
    owner = root.find("reportingOwner")
    name = _t(owner, "reportingOwnerId/rptOwnerName") or "unknown"
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if rel is not None:
        if (_t(rel, "isDirector") or "0") in ("1", "true"):
            roles.append("director")
        if (_t(rel, "isOfficer") or "0") in ("1", "true"):
            roles.append(_t(rel, "officerTitle") or "officer")
        if (_t(rel, "isTenPercentOwner") or "0") in ("1", "true"):
            roles.append("10% owner")
    f = Form4(cik, symbol)
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _t(tx, "transactionCoding/transactionCode")
        shares = _t(tx, "transactionAmounts/transactionShares/value")
        date = _t(tx, "transactionDate/value")
        if not (code and shares and date):
            continue
        price = _t(tx, "transactionAmounts/transactionPricePerShare/value")
        foot_plan = "10b5-1" in ET.tostring(tx, encoding="unicode").lower()
        f.transactions.append(Form4Txn(
            symbol=symbol, issuer_cik=cik, insider_name=name, insider_role=", ".join(roles) or None, txn_code=code,
            acquired_disposed=_t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") or "A",
            shares=float(shares), price=float(price) if price else None, txn_date=dt.date.fromisoformat(date[:10]),
            is_10b5_1=plan or foot_plan,
        ))
    return f


def fetch_form4(http: HttpClient, ua: str, entry: FeedEntry) -> Form4 | None:
    listing = http.get_json("sec_edgar", entry.folder + "/index.json", headers={"User-Agent": ua})
    names = [i["name"] for i in listing.get("directory", {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml") and n.lower() != "filingsummary.xml"]
    for name in xmls[:3]:
        text = http.get_text("sec_edgar", f"{entry.folder}/{name}", headers={"User-Agent": ua})
        if "<ownershipDocument" in text and (f := parse_form4(text)) is not None:
            return f
    return None


OFFERING_FORMS = ("S-1", "S-1/A", "S-1MEF", "F-1", "F-1/A", "F-1MEF", "424B1", "424B2", "424B3", "424B4", "424B5",
                  "424B7", "S-3ASR", "F-3ASR")


def recent_filings(http: HttpClient, ua: str, cik: str, since: dt.date
                   ) -> list[tuple[dt.date, str, tuple[str, ...], str]]:
    """(filing date, form, 8-K items, index URL) filed on or after `since` (SEC submissions API)."""
    data = http.get_json("sec_edgar", f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                         headers={"User-Agent": ua})
    r = data.get("filings", {}).get("recent", {})
    out = []
    for form, date, items, acc in zip(r.get("form", []), r.get("filingDate", []), r.get("items", []),
                                      r.get("accessionNumber", []), strict=False):
        d = dt.date.fromisoformat(date)
        if d < since:
            break
        url = f"{ARCHIVES}/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"
        out.append((d, form, tuple(x.strip() for x in (items or "").split(",") if x.strip()), url))
    return out


FINANCING = re.compile(r"\b(?:offering|private\s+placement|registered\s+direct|pricing\s+of|priced|"
                       r"securities\s+purchase\s+agreement|pipe\s+financing|at-the-market)\b", re.I)


def had_recent_financing(http: HttpClient, ua: str, cik: str, since: dt.date) -> bool:
    """True if the issuer sold stock recently: an IPO/offering registration or prospectus, an 8-K
    Item 3.02, or an 8-K press release announcing an offering or placement. Insiders often buy
    in those at the deal price, which is financing, not open-market conviction."""
    for _, form, items, index_url in recent_filings(http, ua, cik, since):
        if form in OFFERING_FORMS or "3.02" in items:
            return True
        if form.startswith("8-K") and set(items) & {"8.01", "7.01", "1.01"}:
            docs = documents(http.get_text("sec_edgar", index_url, headers={"User-Agent": ua}))
            for kind, url in docs:
                if kind.upper().startswith("EX-99"):
                    h = headline_from_html(http.get_text("sec_edgar", url, headers={"User-Agent": ua})) or ""
                    if FINANCING.search(h):
                        return True
                    break
    return False
