#!/usr/bin/env python3
"""CatalystEdge source verifier.

Calls each data provider with YOUR keys and records what the provider actually
enforces: HTTP status, rate-limit headers, plan-gated endpoints, articles per
request and how far back history goes. The results are compared with the
figures documented in docs/ARCHITECTURE.md, and each ⚠ item is marked
confirmed / differs / unknown.

Standard library only, so it runs anywhere with Python 3.10+:

    python3 scripts/verify_sources.py                 # all sources, reads .env
    python3 scripts/verify_sources.py --only finnhub,sec
    python3 scripts/verify_sources.py --env-file path/to/.env --out reports/

Quota cost: about 20 requests in total, with at most 3 against Alpha Vantage
(25/day free) and 2 against Marketaux (100/day free). It never sends email and
never bursts; calls are spaced at least 1.1 s apart per host.

Keys are read from the environment (or --env-file) and are redacted from
everything this script prints or writes.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

UTC = dt.timezone.utc
TIMEOUT_S = 20
MIN_GAP_S = 1.1

KEY_VARS = [
    "FINNHUB_API_KEY",
    "MARKETAUX_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TIINGO_API_KEY",
    "OPENFDA_API_KEY",
    "FRED_API_KEY",
    "STOOQ_API_KEY",
    "RESEND_API_KEY",
]


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Response:
    status: int  # 0 means a network error before any HTTP status
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    error: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError:
            return None

    def text(self, limit: int = 300) -> str:
        return self.body[:limit].decode("utf-8", "replace")


Fetcher = Callable[[str, dict[str, str]], Response]


def urllib_fetch(url: str, headers: dict[str, str]) -> Response:
    req = urllib.request.Request(url, headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read(2_000_000)
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()},
                            body, int((time.monotonic() - start) * 1000))
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()},
                        e.read(20_000) if e.fp else b"",
                        int((time.monotonic() - start) * 1000))
    except Exception as e:  # DNS, TLS, proxy denial, timeout
        return Response(0, {}, b"", int((time.monotonic() - start) * 1000),
                        error=f"{type(e).__name__}: {e}")


class Client:
    """Polite client: per-host spacing, call counting, secret redaction."""

    def __init__(self, fetch: Fetcher, secrets: list[str], sleep=time.sleep,
                 user_agent: str = "CatalystEdge-verify/1.0"):
        self._fetch = fetch
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self.secrets = [s for s in secrets if s]
        self.calls: dict[str, int] = {}
        self.user_agent = user_agent

    def get(self, source: str, url: str, headers: dict[str, str] | None = None) -> Response:
        host = urllib.parse.urlsplit(url).netloc
        wait = MIN_GAP_S - (time.monotonic() - self._last.get(host, -1e9))
        if wait > 0:
            self._sleep(wait)
        self._last[host] = time.monotonic()
        self.calls[source] = self.calls.get(source, 0) + 1
        h = {"User-Agent": self.user_agent, "Accept": "application/json"}
        h.update(headers or {})
        return self._fetch(url, h)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Check:
    item: str          # what is being verified
    documented: str    # what docs/ARCHITECTURE.md says
    observed: str      # what the API actually did
    verdict: str       # confirmed | differs | unknown | info
    flagged: bool = False  # was this a ⚠ item in the docs?


@dataclasses.dataclass
class SourceResult:
    source: str
    status: str        # ok | failed | skipped | blocked
    note: str = ""
    checks: list[Check] = dataclasses.field(default_factory=list)
    rate_headers: dict[str, str] = dataclasses.field(default_factory=dict)
    calls: int = 0


RATE_HEADER = re.compile(r"(rate-?limit|x-ratelimit|retry-after|x-api-calls|quota)", re.I)


def rate_headers(r: Response) -> dict[str, str]:
    return {k: v for k, v in r.headers.items() if RATE_HEADER.search(k)}


def fail_reason(r: Response) -> str:
    if r.status == 0:
        return f"network error ({r.error})"
    return f"HTTP {r.status}: {r.text(160)}"


def is_blocked(r: Response) -> bool:
    """Network-level denial (proxy/firewall), as opposed to the API refusing us."""
    return r.status == 0


def date_of(value: Any) -> dt.date | None:
    """Parse the date formats these APIs use: ISO strings, AV's 20220301T1200, epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, UTC).date()
    s = str(value)
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Probes (one function per source)                                             #
# --------------------------------------------------------------------------- #


def probe_finnhub(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("finnhub", "ok")
    base = "https://finnhub.io/api/v1"
    q = lambda path, **p: f"{base}{path}?{urllib.parse.urlencode({**p, 'token': key})}"

    r = c.get("finnhub", q("/quote", symbol="AAPL"))
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.rate_headers = rate_headers(r)
    limit = r.headers.get("x-ratelimit-limit")
    res.checks.append(Check("Rate limit", "~60 calls/min",
                            f"x-ratelimit-limit={limit}" if limit else "no rate-limit header",
                            "confirmed" if limit == "60" else ("differs" if limit else "unknown")))
    quote = r.json() or {}
    res.checks.append(Check("/quote returns today's open (used for 09:35 fills)", "yes",
                            f"o={quote.get('o')}", "confirmed" if quote.get("o") else "differs"))

    # History depth: ask for a window ~13 months ago; empty => < 1 year of news.
    frm, to = today - dt.timedelta(days=400), today - dt.timedelta(days=380)
    r = c.get("finnhub", q("/company-news", symbol="AAPL", **{"from": frm.isoformat(), "to": to.isoformat()}))
    items = r.json() if r.status == 200 else None
    n = len(items) if isinstance(items, list) else 0
    res.checks.append(Check("Company-news history depth", "≈ 1 year on free tier",
                            f"{n} items for {frm}..{to}" if r.status == 200 else fail_reason(r),
                            "differs" if n > 0 else ("confirmed" if r.status == 200 else "unknown"),
                            flagged=True))

    # Candles: documented as 403 on free keys.
    r = c.get("finnhub", q("/stock/candle", symbol="AAPL", resolution="D",
                           **{"from": int(time.time()) - 30 * 86400, "to": int(time.time())}))
    res.checks.append(Check("/stock/candle on free key", "403 (premium only)",
                            f"HTTP {r.status}", "confirmed" if r.status == 403 else "differs"))

    r = c.get("finnhub", q("/calendar/earnings", **{"from": today.isoformat(),
                                                   "to": (today + dt.timedelta(days=7)).isoformat()}))
    cal = (r.json() or {}).get("earningsCalendar") if r.status == 200 else None
    res.checks.append(Check("Earnings calendar on free key", "available",
                            f"{len(cal)} rows next 7 days" if isinstance(cal, list) else fail_reason(r),
                            "confirmed" if isinstance(cal, list) else "differs"))
    res.checks.append(Check("Paid plan price", "≈ $50/mo (unconfirmed)",
                            "not visible via API; check finnhub.io/pricing in a browser",
                            "unknown", flagged=True))
    return res


def probe_marketaux(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("marketaux", "ok")
    base = "https://api.marketaux.com/v1/news/all"
    r = c.get("marketaux", f"{base}?" + urllib.parse.urlencode(
        {"symbols": "AAPL", "filter_entities": "true", "language": "en", "limit": 50, "api_token": key}))
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.rate_headers = rate_headers(r)
    body = r.json() or {}
    meta, data = body.get("meta", {}), body.get("data", [])
    res.checks.append(Check("Articles per request (asked for 50)", "3 on free plan",
                            f"meta.limit={meta.get('limit')}, returned={len(data)}",
                            "confirmed" if meta.get("limit") == 3 else "differs"))
    res.checks.append(Check("Daily request limit", "100/day",
                            "headers: " + (json.dumps(res.rate_headers) or "none"),
                            "unknown" if not res.rate_headers else "info"))

    old = today - dt.timedelta(days=730)
    r = c.get("marketaux", f"{base}?" + urllib.parse.urlencode(
        {"symbols": "AAPL", "published_before": old.isoformat(), "language": "en", "api_token": key}))
    data = (r.json() or {}).get("data", []) if r.status == 200 else []
    oldest = min((date_of(d.get("published_at")) for d in data if d.get("published_at")), default=None)
    res.checks.append(Check("History depth (asked for items before 2 years ago)", "undocumented",
                            f"{len(data)} items, oldest {oldest}" if r.status == 200 else fail_reason(r),
                            "info" if r.status == 200 else "unknown", flagged=True))
    res.checks.append(Check("Paid plan price", "unknown", "check marketaux.com/pricing in a browser",
                            "unknown", flagged=True))
    return res


def probe_alphavantage(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("alphavantage", "ok", note="uses 3 of your 25 daily calls")
    q = lambda **p: "https://www.alphavantage.co/query?" + urllib.parse.urlencode({**p, "apikey": key})

    def av_error(body: Any) -> str | None:
        if isinstance(body, dict):
            for k in ("Information", "Note", "Error Message"):
                if k in body:
                    return str(body[k])[:200]
        return None

    r = c.get("alphavantage", q(function="NEWS_SENTIMENT", tickers="AAPL",
                                time_from="20220301T0000", sort="EARLIEST", limit=5))
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    body = r.json()
    err = av_error(body)
    feed = (body or {}).get("feed", []) if not err else []
    earliest = min((date_of(a.get("time_published")) for a in feed), default=None)
    res.checks.append(Check("NEWS_SENTIMENT history start", "Mar 2022",
                            f"earliest item {earliest}" if feed else (err or "empty feed"),
                            "confirmed" if earliest and earliest <= dt.date(2022, 6, 1) else "unknown",
                            flagged=True))

    r = c.get("alphavantage", q(function="TIME_SERIES_DAILY", symbol="IBM", outputsize="full"))
    body = r.json()
    err = av_error(body)
    series = (body or {}).get("Time Series (Daily)", {}) if not err else {}
    if series:
        observed, verdict = f"free: {len(series)} bars back to {min(series)}", "differs"
    elif err and "premium" in err.lower():
        observed, verdict = "premium-only: " + err[:120], "confirmed"
    else:
        observed, verdict = err or fail_reason(r), "unknown"
    res.checks.append(Check("TIME_SERIES_DAILY outputsize=full", "may need premium",
                            observed, verdict, flagged=True))

    r = c.get("alphavantage", q(function="EARNINGS", symbol="IBM"))
    body = r.json()
    err = av_error(body)
    qs = (body or {}).get("quarterlyEarnings", []) if not err else []
    with_est = [x for x in qs if x.get("estimatedEPS") not in (None, "None", "")]
    oldest = min((date_of(x.get("reportedDate")) for x in with_est), default=None)
    res.checks.append(Check("EARNINGS history with estimates (for backtest)", "many years",
                            f"{len(with_est)} quarters with estimates, oldest {oldest}" if qs else (err or fail_reason(r)),
                            "confirmed" if len(with_est) >= 40 else ("differs" if qs else "unknown")))
    res.checks.append(Check("Daily limit", "25/day",
                            "not exposed in headers; a 26th call returns an 'Information' message",
                            "info"))
    return res


def probe_tiingo(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("tiingo", "ok")
    h = {"Authorization": f"Token {key}", "Content-Type": "application/json"}
    r = c.get("tiingo", "https://api.tiingo.com/api/test", h)
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.rate_headers = rate_headers(r)

    r = c.get("tiingo", "https://api.tiingo.com/tiingo/daily/aapl/prices?startDate=1980-01-01&resampleFreq=annually", h)
    rows = r.json() if r.status == 200 else None
    first = date_of(rows[0].get("date")) if isinstance(rows, list) and rows else None
    res.checks.append(Check("EOD history depth (primary price source)", "decades",
                            f"AAPL from {first}" if first else fail_reason(r),
                            "confirmed" if first and first.year < 1990 else "unknown"))

    # Delisted ticker support matters for survivorship-free backtests.
    r = c.get("tiingo", "https://api.tiingo.com/tiingo/daily/lehmq", h)
    meta = r.json() if r.status == 200 else None
    res.checks.append(Check("Delisted tickers available (LEHMQ)", "yes",
                            f"endDate={meta.get('endDate')}" if isinstance(meta, dict) else fail_reason(r),
                            "confirmed" if isinstance(meta, dict) and meta.get("endDate") else "unknown"))

    r = c.get("tiingo", "https://api.tiingo.com/tiingo/news?tickers=aapl&limit=1", h)
    res.checks.append(Check("News API on your plan", "Power plan only (not confirmed for free)",
                            f"HTTP {r.status}: {r.text(120)}" if r.status != 200 else "available",
                            "confirmed" if r.status in (401, 403) else ("differs" if r.status == 200 else "unknown"),
                            flagged=True))
    res.checks.append(Check("Free limits", "50 req/h, 1,000 req/day, 500 symbols/month",
                            "headers: " + (json.dumps(res.rate_headers) or "none"),
                            "info"))
    return res


def probe_sec(c: Client, user_agent: str, today: dt.date) -> SourceResult:
    res = SourceResult("sec_edgar", "ok")
    if not user_agent or "@" not in user_agent:
        res.status, res.note = "skipped", "SEC_USER_AGENT must be 'Name email@example.com' (SEC policy)"
        return res
    h = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    r = c.get("sec_edgar", "https://data.sec.gov/submissions/CIK0000320193.json", h)
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    recent = ((r.json() or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    res.checks.append(Check("Submissions API with declared User-Agent", "works, 10 req/s max",
                            f"{len(forms)} recent filings, {forms.count('4')} Form 4, {forms.count('8-K')} 8-K",
                            "confirmed" if forms else "differs"))
    r = c.get("sec_edgar", "https://www.sec.gov/Archives/edgar/daily-index/2004/QTR1/", h)
    res.checks.append(Check("Deep archive reachable (2004 daily index)", "yes",
                            f"HTTP {r.status}", "confirmed" if r.status == 200 else "unknown"))
    res.checks.append(Check("Rate limit", "10 req/s (policy, not a header)",
                            "not probed on purpose: bursting risks a 10-minute IP block", "info"))
    return res


def probe_openfda(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("openfda", "ok")
    params = {"search": "submissions.submission_status:AP", "limit": 1}
    if key:
        params["api_key"] = key
    r = c.get("openfda", "https://api.fda.gov/drug/drugsfda.json?" + urllib.parse.urlencode(params))
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.rate_headers = rate_headers(r)
    total = ((r.json() or {}).get("meta") or {}).get("results", {}).get("total")
    res.checks.append(Check("drugsfda approvals endpoint", "works", f"total={total}",
                            "confirmed" if total else "unknown"))
    lim = res.rate_headers.get("x-ratelimit-limit")
    res.checks.append(Check("Rate limit" + (" (with key)" if key else " (no key)"),
                            "240/min; daily cap 1,000 without key, higher with key",
                            f"x-ratelimit-limit={lim}, remaining={res.rate_headers.get('x-ratelimit-remaining')}"
                            if lim else "no rate-limit header",
                            "info" if lim else "unknown", flagged=bool(key)))
    return res


def probe_clinicaltrials(c: Client, today: dt.date) -> SourceResult:
    res = SourceResult("clinicaltrials", "ok")
    r = c.get("clinicaltrials", "https://clinicaltrials.gov/api/v2/studies?pageSize=1&fields=NCTId")
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.rate_headers = rate_headers(r)
    res.checks.append(Check("Rate limit", "≈ 50 req/min per IP (third-party reported)",
                            json.dumps(res.rate_headers) if res.rate_headers else "no rate-limit header",
                            "info" if res.rate_headers else "unknown", flagged=True))
    return res


def probe_fred(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("fred", "ok")
    r = c.get("fred", "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(
        {"series_id": "VIXCLS", "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 1}))
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    obs = (r.json() or {}).get("observations", [])
    res.checks.append(Check("VIXCLS latest observation", "works",
                            f"{obs[0].get('date')}={obs[0].get('value')}" if obs else "empty",
                            "confirmed" if obs else "unknown"))
    return res


def probe_stooq(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("stooq (FRAGILE)", "ok")
    params = {"s": "aapl.us", "i": "d"}
    if key:
        params["apikey"] = key
    r = c.get("stooq", "https://stooq.com/q/d/l/?" + urllib.parse.urlencode(params), {"Accept": "text/csv"})
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    text = r.text(200)
    got_csv = text.lower().startswith("date,")
    res.checks.append(Check("CSV download" + (" with apikey" if key else " without apikey"),
                            "apikey required since ≈ Apr 2026",
                            "CSV returned" if got_csv else f"no CSV: {text[:100]!r}",
                            ("confirmed" if not got_csv else "differs") if not key
                            else ("confirmed" if got_csv else "unknown"),
                            flagged=True))
    return res


def probe_resend(c: Client, key: str, today: dt.date) -> SourceResult:
    res = SourceResult("resend", "ok", note="read-only check, no email is sent")
    r = c.get("resend", "https://api.resend.com/domains", {"Authorization": f"Bearer {key}"})
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    domains = (r.json() or {}).get("data", [])
    verified = [d.get("name") for d in domains if d.get("status") == "verified"]
    res.checks.append(Check("API key valid; verified sender domains", "at least one for real alerts",
                            ", ".join(verified) or "none verified (test sender can only email you)",
                            "confirmed" if verified else "info"))
    return res


def probe_huggingface(c: Client, today: dt.date) -> SourceResult:
    res = SourceResult("huggingface (model download)", "ok")
    r = c.get("huggingface", "https://huggingface.co/api/models/ProsusAI/finbert")
    if is_blocked(r) or r.status >= 400:
        res.status = "blocked" if is_blocked(r) else "failed"
        res.note = fail_reason(r)
        return res
    res.checks.append(Check("ProsusAI/finbert downloadable", "yes",
                            f"downloads={((r.json() or {}).get('downloads'))}", "confirmed"))
    return res


# --------------------------------------------------------------------------- #
# Orchestration and report                                                     #
# --------------------------------------------------------------------------- #

DISABLED = {
    "benzinga": "disabled stub (your decision, revisit after Phase 1)",
    "investing_rss": "disabled stub (ToS forbids storing data without written permission)",
}


def run(env: dict[str, str], only: set[str] | None, fetch: Fetcher = urllib_fetch,
        sleep=time.sleep, today: dt.date | None = None) -> tuple[list[SourceResult], Client]:
    today = today or dt.datetime.now(UTC).date()
    c = Client(fetch, [env.get(k, "") for k in KEY_VARS], sleep=sleep)
    k = env.get

    plan: list[tuple[str, str | None, Callable[[], SourceResult]]] = [
        ("finnhub", "FINNHUB_API_KEY", lambda: probe_finnhub(c, k("FINNHUB_API_KEY"), today)),
        ("marketaux", "MARKETAUX_API_KEY", lambda: probe_marketaux(c, k("MARKETAUX_API_KEY"), today)),
        ("alphavantage", "ALPHAVANTAGE_API_KEY", lambda: probe_alphavantage(c, k("ALPHAVANTAGE_API_KEY"), today)),
        ("tiingo", "TIINGO_API_KEY", lambda: probe_tiingo(c, k("TIINGO_API_KEY"), today)),
        ("sec", None, lambda: probe_sec(c, k("SEC_USER_AGENT", ""), today)),
        ("openfda", None, lambda: probe_openfda(c, k("OPENFDA_API_KEY", ""), today)),
        ("clinicaltrials", None, lambda: probe_clinicaltrials(c, today)),
        ("fred", "FRED_API_KEY", lambda: probe_fred(c, k("FRED_API_KEY"), today)),
        ("stooq", None, lambda: probe_stooq(c, k("STOOQ_API_KEY", ""), today)),
        ("resend", "RESEND_API_KEY", lambda: probe_resend(c, k("RESEND_API_KEY"), today)),
        ("huggingface", None, lambda: probe_huggingface(c, today)),
    ]
    results: list[SourceResult] = []
    for name, key_var, probe in plan:
        if only and name not in only:
            continue
        if key_var and not k(key_var):
            results.append(SourceResult(name, "skipped", note=f"{key_var} not set"))
            continue
        before = sum(c.calls.values())
        try:
            r = probe()
        except Exception as e:  # a bug in one probe must not hide the others
            r = SourceResult(name, "failed", note=f"probe crashed: {type(e).__name__}: {e}")
        r.calls = sum(c.calls.values()) - before
        results.append(r)
    for name, why in DISABLED.items():
        if not only or name in only:
            results.append(SourceResult(name, "skipped", note=why))
    return results, c


def to_markdown(results: list[SourceResult], c: Client, when: dt.datetime) -> str:
    out = [f"# CatalystEdge source check · {when:%Y-%m-%d %H:%M} UTC", ""]
    out += ["| Source | Status | Calls | Note |", "|---|---|---|---|"]
    for r in results:
        out.append(f"| {r.source} | {r.status} | {r.calls} | {r.note} |")
    flagged = [(r.source, ch) for r in results for ch in r.checks if ch.flagged]
    if flagged:
        out += ["", "## ⚠ items from the docs", "", "| Source | Item | Documented | Observed | Verdict |",
                "|---|---|---|---|---|"]
        out += [f"| {s} | {ch.item} | {ch.documented} | {ch.observed} | **{ch.verdict}** |" for s, ch in flagged]
    out += ["", "## All checks", ""]
    for r in results:
        if not r.checks and not r.rate_headers:
            continue
        out += [f"### {r.source}", "", "| Item | Documented | Observed | Verdict |", "|---|---|---|---|"]
        out += [f"| {ch.item}{' ⚠' if ch.flagged else ''} | {ch.documented} | {ch.observed} | {ch.verdict} |"
                for ch in r.checks]
        if r.rate_headers:
            out += ["", "Rate-limit headers: `" + json.dumps(r.rate_headers) + "`"]
        out.append("")
    if any(r.status == "blocked" for r in results):
        out += ["", "> **blocked** means the request never reached the provider (DNS, firewall or proxy "
                "denial). It says nothing about the provider's limits. Run this script from a machine "
                "with normal internet access."]
    return c.redact("\n".join(out)) + "\n"


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=".env", type=Path)
    ap.add_argument("--only", help="comma-separated: finnhub,marketaux,alphavantage,tiingo,sec,"
                                   "openfda,clinicaltrials,fred,stooq,resend,huggingface")
    ap.add_argument("--out", default="reports", type=Path)
    args = ap.parse_args(argv)

    env = {**load_env_file(args.env_file), **{k: v for k, v in os.environ.items() if v}}
    only = set(args.only.split(",")) if args.only else None
    results, client = run(env, only)
    now = dt.datetime.now(UTC)
    md = to_markdown(results, client, now)

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"source_check_{now:%Y%m%d_%H%M}"
    stem.with_suffix(".md").write_text(md)
    stem.with_suffix(".json").write_text(client.redact(json.dumps(
        [dataclasses.asdict(r) for r in results], indent=2, default=str)))
    print(md)
    print(f"Report written to {stem}.md and {stem}.json")
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
