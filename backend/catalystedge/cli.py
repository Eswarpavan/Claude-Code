"""Command line: `catalystedge <command>` (or `python -m catalystedge <command>`).

  sample-news        fetch headlines and print ticker, sentiment and event type (sanity check)
  eval-sentiment     compare the sentiment models on the labelled headlines
  compare-sentiment  FinBERT vs the word list, side by side on real headlines (downloads FinBERT)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import textwrap
from pathlib import Path
from zoneinfo import ZoneInfo

from catalystedge.adapters.news.registry import build_news_adapters
from catalystedge.clock import FrozenClock, SystemClock
from catalystedge.config import Settings
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.core.kv import InMemoryKV
from catalystedge.ml.registry import SENTIMENT_MODELS, ModelRegistry
from catalystedge.pipeline.dedupe import normalize_url
from catalystedge.pipeline.noise import FILTERS
from catalystedge.pipeline.run import Processed, process_news
from catalystedge.reference import load_universe

ET = ZoneInfo("America/New_York")
DEFAULT_WATCHLIST = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD"


def _age(now: dt.datetime, t: dt.datetime) -> str:
    h = (now - t).total_seconds() / 3600
    return f"{h:4.1f}h"


def _event_text(p: Processed) -> tuple[str, str]:
    if not p.events:
        return "-", "-"
    parts = []
    for e in p.events:
        tag = e.event_type if e.event_type != "other" else "none"
        parts.append(f"{e.symbol}:{tag}/{e.polarity}")
    eligible = any(e.is_signal_eligible for e in p.events)
    return "; ".join(parts), "YES" if eligible else "no"


def _tz_check(processed: list[Processed]) -> str:
    """Median gap between Alpha Vantage and another provider's timestamp for the same URL."""
    gaps = []
    for p in processed:
        by_url: dict[str, dict[str, dt.datetime]] = {}
        for m in p.cluster.members:
            by_url.setdefault(normalize_url(m.url), {})[m.source_key] = m.published_at
        for times in by_url.values():
            if "alphavantage_news" in times:
                others = [t for s, t in times.items() if s != "alphavantage_news"]
                if others:
                    gaps.append((times["alphavantage_news"] - min(others)).total_seconds() / 3600)
    if not gaps:
        return "not enough overlap between Alpha Vantage and other sources to check its timezone"
    med = statistics.median(gaps)
    verdict = "looks right" if abs(med) < 1 else (
        "Alpha Vantage times look like US Eastern: set ALPHAVANTAGE_NEWS_TZ=America/New_York"
        if 3.5 <= -med <= 5.5 else "unexpected offset: please report")
    return f"median gap {med:+.1f}h over {len(gaps)} shared stories: {verdict}"


def cmd_sample_news(args: argparse.Namespace) -> int:
    overrides = {"CATALYSTEDGE_DATA_MODE": "fixtures"} if args.fixtures else {}
    settings = Settings(**overrides)
    if settings.data_mode == "fixtures":
        from catalystedge.fixtures import FixtureTransport, recorded_at

        now = recorded_at()
        clock = FrozenClock(now)
        http = HttpClient(transport=FixtureTransport(), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)
    else:
        clock = SystemClock()
        now = clock.now()
        http = HttpClient(kv=InMemoryKV(clock), clock=clock)
    for secret in (settings.finnhub_api_key, settings.marketaux_api_key, settings.alphavantage_api_key,
                   settings.tiingo_api_key):
        http.register_secret(secret)

    try:
        universe, universe_desc = load_universe(settings, http)
    except SourceError as e:
        print(f"Could not download SEC company list ({http.redact(str(e))}); using offline subset.")
        settings_offline = Settings(CATALYSTEDGE_DATA_MODE="fixtures")
        universe, universe_desc = load_universe(settings_offline, http)

    registry = ModelRegistry(settings)
    model = registry.sentiment()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    processed, reports = process_news(build_news_adapters(settings, http), universe, model, now, symbols)

    mode = "FIXTURES (hand-written sample data, not real news)" if settings.data_mode == "fixtures" else "LIVE"
    print(f"\nCatalystEdge news sample · {mode} · now {now.astimezone(ET):%Y-%m-%d %H:%M} ET")
    print("DIAGNOSTIC VIEW: shows every headline, including neutral and negative ones. The app itself only")
    print("ever shows positive signals, and all confidence stays UNCALIBRATED in Phase 1.\n")

    print("Sources")
    for r in reports:
        extra = f" · {r.error}" if r.error else ""
        print(f"  {r.source_key:<18} {r.status:<9} calls={r.http_calls:<3} fetched={r.fetched:<3} kept={r.kept:<3}"
              f" too_old={r.too_old:<2} future={r.in_future:<2}{extra}")
    st = next((s for s in registry.report() if s.name == settings.sentiment_model), None)
    fallback_note = f"  ({st.status}: {st.detail})" if st and model.name != st.name else ""
    print(f"\nSentiment model: {model.name}{fallback_note}")
    print(f"Ticker universe: {universe_desc}")
    print(f"Alpha Vantage timezone check: {_tz_check(processed)}\n")

    noise = [p for p in processed if p.noise]
    print(f"Non-event filter ({len(noise)} of {len(processed)} stories removed)")
    for reason in FILTERS:
        print(f"  {reason:<24} {sum(p.noise.reason == reason for p in noise):>4}")
    print()

    rows = sorted(processed, key=lambda p: p.cluster.representative.published_at, reverse=True)
    if args.save_headlines:
        kept = [p.headline for p in rows if not p.noise and p.link.mentions]
        Path(args.save_headlines).write_text(json.dumps({"collected_at": now.isoformat(), "headlines": kept},
                                                        indent=1) + "\n")
    if args.show_filtered:
        print("Removed by the non-event filter")
        for p in [p for p in rows if p.noise][: args.limit]:
            print(f"  {p.noise.reason:<24} {p.headline}")
        print()
    rows = [p for p in rows if not p.noise]
    if not args.all:
        rows = [p for p in rows if p.link.mentions]
    rows = rows[: args.limit]
    header = f"{'age':>5}  {'tickers':<11} {'sentiment':<17} {'signal?':<7} headline"
    print(header)
    print("-" * min(140, len(header) + 60))
    for p in rows:
        rep = p.cluster.representative
        tickers = ",".join(p.link.symbols) or "-"
        s = p.sentiment
        senti = f"{s.label[:3]} {s.margin:+.2f}"
        events, eligible = _event_text(p)
        srcs = "+".join(x.split("_")[0] for x in p.cluster.sources)
        print(f"{_age(now, rep.published_at)}  {tickers:<11} {senti:<17} {eligible:<7} {rep.headline}")
        print(textwrap.indent(f"event: {events}   sources: {srcs}", " " * 7))
        mixed = next((e.mixed_resolution for e in p.events if e.mixed_resolution), None)
        if mixed:
            print(" " * 7 + f"mixed: {mixed['conflict']}")
    events = [p for p in processed if not p.noise]
    hidden = len(events) - len([p for p in events if p.link.mentions])
    signals = sum(any(e.is_signal_eligible for e in p.events) for p in events)
    print(f"\n{len(processed)} stories after dedupe ({sum(r.kept for r in reports)} headlines in the 48h window);"
          f" {len(noise)} removed as non-events; {hidden} of the rest had no confidently linked ticker"
          + ("" if args.all else " (use --all to show them)"))
    print(f"Stories that would count as a signal: {signals} (auto-buy OFF; confidence UNCALIBRATED)")
    print(f"API calls used: {sum(r.http_calls for r in reports)}")
    return 0


def cmd_eval_sentiment(args: argparse.Namespace) -> int:
    from catalystedge.ml.eval_sentiment import evaluate
    from catalystedge.ml.sentiment import LexiconSentiment

    settings = Settings()
    print(f"{'model':<18} {'status':<12} {'accuracy':>8} {'macro-F1':>8} {'ms/headline':>11}")
    candidates = [LexiconSentiment()]
    for name in SENTIMENT_MODELS:
        reg = ModelRegistry(Settings(SENTIMENT_MODEL=name))
        m = reg.sentiment()
        if m.name == name:
            candidates.append(m)
        else:
            st = reg.status(name)
            print(f"{name:<18} {st.status:<12} {'-':>8} {'-':>8} {'-':>11}   {st.detail}")
    for m in candidates:
        r = evaluate(m)
        print(f"{m.name:<18} {'ready':<12} {r.accuracy:>8.2f} {r.macro_f1:>8.2f} {r.ms_per_headline:>11.2f}")
    print(f"\nConfigured default: {settings.sentiment_model}. The labelled set was written by CatalystEdge's author;"
          " treat these numbers as a sanity check, not a benchmark.")
    return 0


def cmd_compare_sentiment(args: argparse.Namespace) -> int:
    """FinBERT (or the configured model) vs the word list on the same headlines. No API keys needed."""
    from catalystedge.fixtures import load_json
    from catalystedge.ml.eval_sentiment import evaluate
    from catalystedge.ml.sentiment import LexiconSentiment

    settings = Settings(SENTIMENT_MODEL=args.model)
    data = json.loads(Path(args.headlines).read_text()) if args.headlines else load_json(
        "reference/live_sample_headlines.json")
    headlines = data["headlines"][: args.limit]
    print(f"Loading {args.model} (first run downloads it, about 0.5 GB; this can take a few minutes)...", flush=True)
    registry = ModelRegistry(settings)
    model = registry.sentiment()
    lexicon = LexiconSentiment()
    if model.name == lexicon.name:
        st = registry.status(args.model)
        print(f"\nCould not load {args.model}: {st.status}: {st.detail}")
        print("Nothing to compare. Please send this output back as it is.")
        return 1

    print(f"\nCatalystEdge sentiment comparison · {model.name} vs {lexicon.name}")
    print(f"Headlines: {len(headlines)} live headlines collected {data.get('collected_at', '?')[:16]} UTC\n")
    fin, lex = model.predict(headlines), lexicon.predict(headlines)
    print(f"{model.name + ' (pos/neu/neg)':<30} {'word list':<14} same?  headline")
    print("-" * 120)
    agree = 0
    for h, f, w in zip(headlines, fin, lex, strict=True):
        same = f.label == w.label
        agree += same
        probs = f"{f.label[:3]} {f.positive:.2f}/{f.neutral:.2f}/{f.negative:.2f}"
        print(f"{probs:<30} {w.label[:3] + f' {w.margin:+.2f}':<14} {'yes' if same else 'NO':<6} {h[:110]}")
    print(f"\nSame label on {agree} of {len(headlines)} headlines.")
    print("\nAccuracy on the hand-labelled test headlines:")
    for m in (model, lexicon):
        r = evaluate(m)
        print(f"  {m.name:<18} accuracy {r.accuracy:.2f}  macro-F1 {r.macro_f1:.2f}"
              f"  {r.ms_per_headline:.1f} ms/headline")
    print("\nSentiment is only one input; auto-buy stays OFF and confidence is UNCALIBRATED in Phase 1.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="catalystedge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample-news", help="fetch headlines and print ticker, sentiment, event type")
    s.add_argument("--fixtures", action="store_true", help="use the offline sample data instead of live APIs")
    s.add_argument("--symbols", default=DEFAULT_WATCHLIST, help="watchlist for per-company news")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--all", action="store_true", help="also show headlines with no linked ticker")
    s.add_argument("--show-filtered", action="store_true", help="list headlines the non-event filter removed")
    s.add_argument("--save-headlines", metavar="PATH", help="write the kept, linked headlines to a JSON file")
    s.set_defaults(func=cmd_sample_news)
    c = sub.add_parser("compare-sentiment", help="FinBERT vs the word list on real headlines")
    c.add_argument("--model", default="finbert", choices=sorted(SENTIMENT_MODELS))
    c.add_argument("--headlines", metavar="PATH", help="JSON from sample-news --save-headlines (default: bundled)")
    c.add_argument("--limit", type=int, default=60)
    c.set_defaults(func=cmd_compare_sentiment)
    e = sub.add_parser("eval-sentiment", help="compare sentiment models")
    e.set_defaults(func=cmd_eval_sentiment)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
