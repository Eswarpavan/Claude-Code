# CatalystEdge Phase 1: progress and next steps

*Last updated 2026-09-24. Read this first when continuing Phase 1 in a new session.*

## Where we are

Built and tested in the agreed order. The first six steps are done. The build is
**paused at the agreed sanity-check point**, before the signal engine.

| # | Step | Status | Tests |
|---|---|---|---|
| 0 | Source checker `scripts/verify_sources.py` | done; **run live 2026-09-24** | 16 |
| 1 | Database schema + migrations (0001, 0002), rule-2 DB triggers | done | 18 |
| 2 | 48-hour news window | done | 15 |
| 3 | News adapters, HTTP client (budgets, cache, retries, breaker), dedupe, storage | done | 11 + 16 + 2 |
| 4 | Ticker linking + ambiguity log | done | 29 |
| 5 | Sentiment models behind a registry (FinBERT default, word-list fallback) | done | 26 |
| 6 | Event classification + mixed-headline rules + end-to-end pipeline + `sample-news` | done | 34 + 4 |
| | **Checkpoint: the user sanity-checks real headlines** | **live sample run; waiting on user; 2 bugs found (below)** | |
| 7 | Signal engine (rules, UNCALIBRATED confidence) | not started | |
| 8 | Paper account (auto-buy OFF) | not started | |

Total: **155 backend tests + 16 checker tests, all passing** against Postgres 16.

Unchanged commitments: auto-buy stays OFF (enforced server-side) and every
confidence number is labelled UNCALIBRATED in Phase 1.

## Live run, 2026-09-24 (second session)

Keys present: `FINNHUB_API_KEY`, `TIINGO_API_KEY`, `SEC_USER_AGENT`. Not set:
Marketaux, Alpha Vantage, FRED, openFDA, Stooq, Resend.

Source checker (`reports/source_check_20260924_0704.md`, gitignored):
- **Finnhub ok**: 60 calls/min confirmed from the `x-ratelimit-limit` header; `/quote`
  open works; `/stock/candle` 403 on free key (as documented); earnings calendar works;
  company news about 1 year deep (⚠ resolved). Paid price still unknown (not in the API).
- **Tiingo ok**: EOD works (AAPL from 1980). No rate-limit headers, so published limits
  stay. **Tiingo News returns 403 on the free plan** (⚠ resolved: Power plan only).
- **SEC EDGAR ok**: submissions API and 2004 archive reachable.
- **Blocked by network policy**: api.fda.gov, clinicaltrials.gov, query1.finance.yahoo.com,
  stooq.com, and the Hugging Face file hosts `cas-server.xethub.hf.co` and
  `us.aws.cdn.hf.co`.
- Skipped (no key): Marketaux, Alpha Vantage, FRED, Resend. Alpha Vantage timezone check
  could not run.

No number in `sources.py` needed to change (Finnhub spacing 1.2 s = 50/min, under 60).
ARCHITECTURE §3 now marks live-confirmed cells with ✔ live.

`sample-news` (live): Finnhub only, 9 API calls, 1,412 headlines in 48 h → 1,021 stories
after dedupe. **Sentiment used the word-list fallback**: FinBERT's files are served from
`cas-server.xethub.hf.co` (blocked); `HF_HUB_DISABLE_XET=1` falls back to
`us.aws.cdn.hf.co`, also blocked.

### Problems the live sample exposed (fix before step 7)

1. **Ticker linking gives many false links.** Two causes:
   - `make_aliases` in `pipeline/ticker_link.py` turns the first word of a company name
     into a single-word alias. With title-case headlines, ordinary words match:
     "Where" → WFCF, "Stock" → SYBT, "Here's" → HERE, "On" → ONON, "Trump" → DJT,
     "Hold" → HMELF, "Connect" → CNTB, "Brilliant" → BRLT, "Dow" → DOW, "Nasdaq" → NDAQ.
     The capital-letter guard does not help in title case.
   - Finnhub `/company-news` items get the query symbol as a provider tag at 0.915
     confidence, above the 0.80 threshold, so off-topic items ("Three Lesser-Known
     401(k) Features") link to NVDA/META.
2. **M&A rule fires on "buy"** (`pipeline/classify.py`, `m_and_a` / `ACQ_ACTIVE`):
   "Palantir Flashes Buy Signal; Trump-Xi On Tap" and "2 Monster Dividend Stocks to
   Buy Now and Hold" became `m_and_a_target` → signal YES for DJT, ONON, HMELF.
   Combined with problem 1 this produces false positive signals.

Proposed fix: single-word aliases only for a curated list of well-known brands (or
when the word is not an English dictionary word and not title-cased context); require
a headline mention (name, ticker, tag) for company-news provider tags; M&A only on
"to acquire / agrees to buy / to be acquired / takeover" patterns with a named target,
not bare "buy". Add the live headlines above as regression tests.

Also fixed this session: `test_live_mode_without_keys_reports_disabled_not_errors`
read real keys from the environment; it now clears them first.

## What was blocking the real-data checkpoint (first session)

The first build session had no API keys and a closed network, so everything
so far ran on **hand-written fixtures** (example.com links, not real news). The
real run needs a session where:

1. these environment variables are set (names only; values never go in chat or git):
   - `FINNHUB_API_KEY` (user has one)
   - `TIINGO_API_KEY` (user has one; used for prices, not news)
   - `SEC_USER_AGENT` = the user's name and email, e.g. `Jane Doe jane@example.com`
     (SEC requires it; it's not a secret)
   - optional: `MARKETAUX_API_KEY`, `ALPHAVANTAGE_API_KEY` (free sign-ups; more news coverage)
2. Network access allows the domains listed below.

The user pasted two keys into the chat of the first session. They were never
written to any file, log or commit, but the user was advised to regenerate
them.

### Domains to allow

Needed for the checkpoint:

    finnhub.io
    api.tiingo.com
    www.sec.gov
    data.sec.gov

Needed for the rest of the source checker (optional now):

    api.marketaux.com
    www.alphavantage.co
    api.fda.gov
    clinicaltrials.gov
    api.stlouisfed.org
    query1.finance.yahoo.com
    query2.finance.yahoo.com
    stooq.com
    api.resend.com

Needed to use FinBERT instead of the word-list fallback (optional; about 1 GB of downloads):

    huggingface.co
    cdn-lfs.huggingface.co
    cdn-lfs.hf.co
    cas-bridge.xethub.hf.co
    cas-server.xethub.hf.co
    us.aws.cdn.hf.co

If the Hugging Face download still fails, the sample falls back to the word
list and says so on screen.

## What the next session should do

```bash
cd backend && uv sync                       # add --extra ml to try FinBERT (large download)
python3 ../scripts/verify_sources.py        # real limits -> reports/source_check_*.md
uv run catalystedge sample-news             # live headlines: ticker, sentiment, event type
```

Then:
1. Show the user the sample (plain English), and ask them to sanity-check it.
2. Update `docs/ARCHITECTURE.md` §3 and `backend/catalystedge/sources.py`
   (budgets, spacing, poll cadence) to match the checker's real limits. Resolve
   each ⚠ item.
3. If the Alpha Vantage timezone check says Eastern, set
   `ALPHAVANTAGE_NEWS_TZ=America/New_York`.
4. Only after the user approves, start step 7 (signal engine).

To see the pipeline without keys: `uv run catalystedge sample-news --fixtures --all`.

## Running the tests

```bash
cd backend
# DB tests need a Postgres 16 database; they are skipped without TEST_DATABASE_URL
export TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/catalystedge_test
uv run pytest
uv run ruff check catalystedge tests
python3 ../scripts/test_verify_sources.py
```
