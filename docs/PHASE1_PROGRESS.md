# CatalystEdge Phase 1: progress and next steps

*Last updated 2026-09-24 ~09:45 UTC. Read this first when continuing in a new session.*

## Where we are (updated ~09:45 UTC, session 01Ny)

Two Claude sessions build this branch in parallel; the split is in `docs/SESSION_SPLIT.md`
(01Ny = signal brain, 018K = app/infrastructure). Single-user app.

| Area | Status | Where |
|---|---|---|
| News pipeline, linking, classification, non-event filter | done, live-tested | `pipeline/` |
| FinBERT policy: rule decides; `model_disagrees` flag lowers confidence; veto only >= 0.90 with no rule catalyst | done | `pipeline/classify.py` (rules-v3) |
| Signal engine: priors (upgrades low weight), point-in-time features, priced-in skip, transparent rule score, ranker/calibrator/TimesFM hooks | done, live-tested | `signals/` |
| SEC 8-K + Form 4 (directors/officers, no financings/IPOs), earnings surprises, openFDA into the same events table | done, live-tested | `events/`, `adapters/events/sec.py` |
| ClinicalTrials.gov | code done; **blocked by network** (clinicaltrials.gov) | `events/trials.py` |
| Backtest (EDGAR-based), walk-forward, LightGBM, calibration, SHAP, baselines, by-catalyst report | code done + tested on synthetic data; **real run waiting for data download** | `backtest/`, `ml/ranker.py` |
| TimesFM 3.0 toggle, background job, cache, fallback, backtest with/without | done; model downloaded and health check passes | `ml/timeseries.py` |
| Paper account, outcomes, alerts, scheduler, refresh, API + login, dashboard, Docker, CI | 018K (see their commits) | `paper/`, `jobs.py`, `api/`, `frontend/` |

Tests: **398 backend tests passing** against Postgres 16 (both sessions).

### Backtest data download (resumable, runs in the background)
```bash
cd backend
export DATABASE_URL=...  BACKTEST_CACHE=./data_cache/backtest   # data_cache is gitignored
uv run python -m catalystedge.backtest.fetch sec        # SEC history, ~2.5 req/s per worker
uv run python -m catalystedge.backtest.fetch prices     # Tiingo, 50/h free limit -> ~2.5 h for 106 names
uv run python -m catalystedge.backtest.fetch earnings   # Finnhub, last 4 quarters per name (done)
uv run python -m catalystedge.backtest.fetch fda        # openFDA approvals (done)
uv run catalystedge backtest --timesfm                  # walk-forward + ranker + calibration + TimesFM
```
Re-running any fetch skips what is already cached.

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

### Fixes after the first live sample (done, same day)

All wrong examples below are regression tests in `tests/test_live_regressions.py`.

- **Ticker linking** (`pipeline/ticker_link.py`): one-word company names that are ordinary
  English words never link by name (`data/common_word_names.txt`, 802 words, regenerate with
  `scripts/build_common_word_names.py`; famous brands like Apple/Amazon/Intel are allowlisted).
  Acronym names ("SU", "GPT") must match in capitals; every word of a multi-word name must be
  capitalised; hand-picked aliases (Apple, Meta, Google) beat look-alike companies; share-class
  tickers (GOOG) link as bare tickers. Finnhub `/company-news` tags are now a weak hint
  (score 0.0 → 0.60) that only corroborates a headline mention.
- **M&A** (`pipeline/classify.py`, now `rules-v2`): only deal wording ("to acquire", "agrees
  to buy", "to be acquired", "takeover bid", "receives buyout offer"). Bare "buy" never counts.
- **Other false catalysts found on the same run**: "upgrade" must be an analyst action (not
  "Power Upgrade Agreement"); a listed broker (Stifel, Oppenheimer...) is never the upgraded
  company; "Top Analyst Forecasts" is not a beat; a price target is not company guidance
  ("Lifts Target To $900" is an upgrade).
- **Non-event filter** (`pipeline/noise.py`): listicle, stock_picking_advice,
  long_range_speculation (always), and price_move_only, market_commentary, opinion_or_question
  (only when no catalyst phrase is present). Filtered headlines produce no events.
- `sample-news` prints per-filter counts, `--show-filtered`, `--save-headlines`;
  new `compare-sentiment` command (FinBERT vs word list on `fixtures/reference/live_sample_headlines.json`).

Live rerun (Finnhub, 1,016 stories): filter removed 271 (listicle 35, advice 29, speculation 13,
price move 51, market commentary 44, opinion/question 99). Signals: 13 with the word list,
11 with FinBERT, all analyst upgrades / price-target raises.

FinBERT became downloadable mid-session (the network change took effect). Results:
same label as the word list on 36 of 57 live headlines; on the hand-labelled set
word list 0.92, distilroberta 0.84, deberta 0.84, finbert 0.74 (that set was written with the
word list, so it flatters it). **Open issue:** FinBERT scores some price-target-hike headlines
strongly negative ("Meta Stock Scores Price Target Hike" 0.90), and the classifier's model veto
(`MODEL_VETO_NEG = 0.60`) then turns them into "mixed", so they are not signals. Decide before
step 7 whether the veto should apply to analyst actions.

Known leftovers: "AI Era" still links AERA (AI Era Corp); "Chip Trillionaires Club: Only One
Clear Buy Among NVDA, AVGO, MU, AMD" is not filtered; "Tesla wins lead role in 2,500-truck ...
order" is not recognised as a contract win.

### Problems the first live sample exposed (now fixed, kept for the record)

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
