"""Background data collection for the backtest (resumable).

    python -m catalystedge.backtest.fetch sec     # SEC 8-K headlines + Form 4s -> cache dir
    python -m catalystedge.backtest.fetch prices  # Tiingo daily bars -> prices_daily

Tiingo's free plan allows 50 requests/hour, so prices for ~100 symbols take ~2.5 hours;
the runner waits for the next hour when the budget is used up instead of failing.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

from catalystedge.backtest.history import DEFAULT_UNIVERSE, fetch_company
from catalystedge.core.http import BudgetExhausted, SourceError

START = dt.date(2024, 7, 1)          # events from 2024-09; bars from July for 50-day features
BENCHMARKS = ("SPY",)


def cache_dir() -> Path:
    return Path(os.environ.get("BACKTEST_CACHE", "./data_cache/backtest"))


def run_sec(symbols: list[str]) -> None:
    from catalystedge import jobs

    ctx = jobs.build_context()
    ua = ctx.settings.sec_user_agent
    if not ua:
        sys.exit("SEC_USER_AGENT not set")
    universe = ctx.universe()
    for sym in symbols:
        c = universe.by_symbol.get(sym)
        if c is None or not c.cik:
            print(f"{sym}: not in SEC list, skipped", flush=True)
            continue
        for attempt in range(3):
            try:
                fetch_company(ctx.http, ua, sym, c.cik, START, cache_dir(), log=lambda m: print(m, flush=True))
                break
            except SourceError as e:
                print(f"{sym}: {ctx.http.redact(str(e))[:120]} (retry {attempt + 1})", flush=True)
                time.sleep(30)


def run_meta(symbols: list[str]) -> None:
    """SEC industry code + shares-outstanding history per company (for sector / market-cap slices)."""
    from catalystedge import jobs
    from catalystedge.backtest.slices import fetch_meta

    ctx = jobs.build_context()
    ua = ctx.settings.sec_user_agent
    if not ua:
        sys.exit("SEC_USER_AGENT not set")
    universe = ctx.universe()
    for sym in symbols:
        c = universe.by_symbol.get(sym)
        if c is None or not c.cik:
            print(f"{sym}: not in SEC list, skipped", flush=True)
            continue
        try:
            m = fetch_meta(ctx.http, ua, sym, c.cik, cache_dir())
            print(f"{sym}: SIC {m['sic']} ({m['sic_description']}), {len(m['shares'])} share counts", flush=True)
        except SourceError as e:
            print(f"{sym}: {ctx.http.redact(str(e))[:120]}", flush=True)


def run_filings(symbols: list[str]) -> None:
    """Every filing (form, dates, 8-K items) since START per company, for the offering/late-filing slice."""
    import json

    from catalystedge import jobs
    from catalystedge.backtest.history import _submissions

    ctx = jobs.build_context()
    ua = ctx.settings.sec_user_agent
    if not ua:
        sys.exit("SEC_USER_AGENT not set")
    universe = ctx.universe()
    for sym in symbols:
        path = cache_dir() / f"filings_{sym}.json"
        c = universe.by_symbol.get(sym)
        if path.exists() or c is None or not c.cik:
            continue
        try:
            rows = _submissions(ctx.http, ua, c.cik, START)
        except SourceError as e:
            print(f"{sym}: {ctx.http.redact(str(e))[:120]}", flush=True)
            continue
        keep = [[r.get("form", ""), r.get("filingDate", ""), r.get("acceptanceDateTime", ""), r.get("items", "")]
                for r in rows]
        path.write_text(json.dumps(keep))
        print(f"{sym}: {len(keep)} filings", flush=True)


def run_prices(symbols: list[str]) -> None:
    from sqlalchemy import func, select

    from catalystedge import jobs
    from catalystedge.db.models import PriceDaily
    from catalystedge.prices import PriceService, build_price_adapters, store_bars

    ctx = jobs.build_context()
    service = PriceService(build_price_adapters(ctx.settings, ctx.http), ctx.clock)
    for sym in list(BENCHMARKS) + symbols:
        with ctx.session_factory() as s:
            have = s.scalar(select(func.min(PriceDaily.date)).where(PriceDaily.symbol == sym))
            latest = s.scalar(select(func.max(PriceDaily.date)).where(PriceDaily.symbol == sym))
        if have is not None and have <= START + dt.timedelta(days=7) and latest and \
                latest >= ctx.clock.now().date() - dt.timedelta(days=5):
            continue
        for _ in range(12):
            try:
                res = service.daily(sym, START, ctx.clock.now().date())
            except BudgetExhausted:
                res = None
            if res is not None and (res.bars or not any("budget" in e.lower() for e in res.errors.values())):
                break
            print(f"{sym}: Tiingo hourly budget used; waiting 10 min", flush=True)
            time.sleep(600)
        if res is None:
            continue
        with ctx.session_factory() as s:
            n = store_bars(s, res.bars, ctx.clock.now())
            s.commit()
        print(f"{sym}: {n} bars from {res.source} {'' if n else res.errors}", flush=True)


def run_earnings(symbols: list[str]) -> None:
    """Finnhub /stock/earnings: actual vs estimate for recent quarters (free plan: last 4)."""
    import json

    from catalystedge import jobs

    ctx = jobs.build_context()
    key = ctx.settings.finnhub_api_key
    if not key:
        sys.exit("FINNHUB_API_KEY not set")
    cache_dir().mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        path = cache_dir() / f"earnings_{sym}.json"
        if path.exists():
            continue
        try:
            rows = ctx.http.get_json("finnhub_earnings", "https://finnhub.io/api/v1/stock/earnings",
                                     {"symbol": sym, "limit": 12, "token": key})
        except SourceError as e:
            print(f"{sym}: {ctx.http.redact(str(e))[:120]}", flush=True)
            continue
        path.write_text(json.dumps(rows if isinstance(rows, list) else []))
        print(f"{sym}: {len(rows) if isinstance(rows, list) else 0} quarters", flush=True)


def run_fda(symbols: list[str]) -> None:
    """openFDA NDA/BLA original approvals and efficacy supplements since START, linked to tickers."""
    import json
    from zoneinfo import ZoneInfo

    from catalystedge import jobs
    from catalystedge.events.fda import is_material, sponsor_symbol

    ctx = jobs.build_context()
    universe = ctx.universe()
    wanted = set(symbols)
    out, skip = [], 0
    while True:
        try:
            data = ctx.http.get_json("openfda", "https://api.fda.gov/drug/drugsfda.json", {
                "search": f"submissions.submission_status_date:[{START:%Y%m%d} TO {ctx.clock.now():%Y%m%d}] AND "
                          "submissions.submission_status:AP AND (application_number:NDA* OR application_number:BLA*)",
                "limit": 100, "skip": skip})
        except SourceError as e:
            print(f"openFDA: {ctx.http.redact(str(e))[:160]}", flush=True)
            break
        results = data.get("results") or []
        for r in results:
            app = r.get("application_number") or ""
            sym = sponsor_symbol(r.get("sponsor_name") or "", universe)
            if sym not in wanted:
                continue
            product = ", ".join(sorted({p.get("brand_name") for p in r.get("products", []) if p.get("brand_name")}))
            for sub in r.get("submissions") or []:
                d = sub.get("submission_status_date") or ""
                if d >= f"{START:%Y%m%d}" and is_material(app, sub):
                    day = dt.datetime.strptime(d, "%Y%m%d").date()
                    at = dt.datetime.combine(day, dt.time(16, 15), ZoneInfo("America/New_York")).astimezone(dt.UTC)
                    orig = sub.get("submission_type") == "ORIG"
                    out.append({"symbol": sym, "available_at": at.isoformat(), "ref": f"{app}:{d}",
                                "materiality": 0.85 if orig else 0.7,
                                "headline": f"FDA {'approval' if orig else 'new indication'}: {product or app}"})
        skip += 100
        if len(results) < 100 or skip >= 2000:
            break
    cache_dir().mkdir(parents=True, exist_ok=True)
    (cache_dir() / "fda.json").write_text(json.dumps(out))
    print(f"openFDA: {len(out)} approvals for universe tickers", flush=True)


if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else "sec"
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(DEFAULT_UNIVERSE)
    {"sec": run_sec, "prices": run_prices, "earnings": run_earnings, "fda": run_fda, "meta": run_meta,
     "filings": run_filings}[part](syms)
