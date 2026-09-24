# CatalystEdge Phase 1: progress and next steps

*Last updated 2026-09-24. Read this first when continuing Phase 1 in a new session.*

## Where we are

Built and tested in the agreed order. The first six steps are done. The build is
**paused at the agreed sanity-check point**, before the signal engine.

| # | Step | Status | Tests |
|---|---|---|---|
| 0 | Source checker `scripts/verify_sources.py` | done; **not yet run with real keys** | 16 |
| 1 | Database schema + migrations (0001, 0002), rule-2 DB triggers | done | 18 |
| 2 | 48-hour news window | done | 15 |
| 3 | News adapters, HTTP client (budgets, cache, retries, breaker), dedupe, storage | done | 11 + 16 + 2 |
| 4 | Ticker linking + ambiguity log | done | 29 |
| 5 | Sentiment models behind a registry (FinBERT default, word-list fallback) | done | 26 |
| 6 | Event classification + mixed-headline rules + end-to-end pipeline + `sample-news` | done | 34 + 4 |
| | **Checkpoint: the user sanity-checks real headlines** | **waiting** | |
| 7 | Signal engine (rules, UNCALIBRATED confidence) | not started | |
| 8 | Paper account (auto-buy OFF) | not started | |

Total: **155 backend tests + 16 checker tests, all passing** against Postgres 16.

Unchanged commitments: auto-buy stays OFF (enforced server-side) and every
confidence number is labelled UNCALIBRATED in Phase 1.

## What is blocking the real-data checkpoint

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
