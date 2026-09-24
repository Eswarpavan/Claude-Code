# CatalystEdge

CatalystEdge finds **positive, news-driven swing-trade setups** in US stocks and runs a
**$100 paper-trading account** on them. Every signal starts from a fresh news item or filing;
prices only confirm or rank it. It uses end-of-day data only, never trades intraday, and labels
every confidence number **UNCALIBRATED** until backtests or real outcomes support it.
Auto-buy stays **off** until that evidence exists.

> **Not financial advice.** Research and paper-trading tool only.

## What's in the box

| Part | Where |
|---|---|
| Dashboard (Signals, Paper Portfolio, Trade History, News Feed, Backtest & Calibration, Source Health, Settings) | `frontend/` (Next.js) |
| API | `backend/catalystedge/api/` (FastAPI) |
| Scheduler (news polls, end-of-day signals, paper fills at the open, emails) | `backend/catalystedge/worker/` (Celery) |
| News pipeline, ticker linking, sentiment, event classification | `backend/catalystedge/pipeline/`, `ml/` |
| SEC 8-K / Form 4, earnings, FDA, trials | `backend/catalystedge/events/` |
| Signal engine, ranking model, calibration, backtest | `backend/catalystedge/signals/` and related |
| Paper account, outcomes, email alerts | `backend/catalystedge/paper/`, `outcomes/`, `notify/` |
| Design and data sources | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| $0 online deployment | [docs/DEPLOY_FREE.md](docs/DEPLOY_FREE.md) |
| Progress log | [docs/PHASE1_PROGRESS.md](docs/PHASE1_PROGRESS.md) |

## Run it on your computer

1. Install **Docker Desktop** (Windows/Mac) or Docker Engine (Linux).
2. Download this project, open a terminal in its folder, and create your settings file:
   ```bash
   cp .env.example .env
   ```
3. Open `.env` in a text editor and fill in at least `FINNHUB_API_KEY`, `TIINGO_API_KEY` and
   `SEC_USER_AGENT` (your name and email). Everything else is optional.
4. Start everything:
   ```bash
   docker compose up -d
   ```
   **The first start is slow** (it builds the app and downloads the ~0.5 GB FinBERT model).
5. Open **http://localhost:3000**. The Signals page shows cached results at once and refreshes in
   the background; Source Health shows each source and the model's download status.

Stop with `docker compose down` (your data stays in Docker volumes).

**Alerts and paper trades only run while the computer is on.** For alerts while it is off, use an
always-on machine or the free cloud setup in [docs/DEPLOY_FREE.md](docs/DEPLOY_FREE.md).

## Check FinBERT on your computer

No API keys needed. After `docker compose up -d`:
```bash
docker compose run --rm api python -m catalystedge compare-sentiment
```
It downloads FinBERT (first time only), scores recent headlines with FinBERT and with the
word-list fallback side by side, and prints accuracy on hand-labelled headlines. To send the result
back: copy everything it printed and paste it into the chat. It contains no keys or personal data.

## Keeping your API keys safe

1. Put keys **only** in the `.env` file on your machine (or the host's secret settings, e.g. the
   environment settings of a Claude cloud session, Vercel's Environment Variables, or the VM's `.env`).
   `.env` is excluded from git, and the app never prints or logs a key.
2. Never paste a key into a chat, an email, a screenshot, or an issue. If that happens, generate a
   new key on the provider's website and replace the old one in `.env`.
3. On a shared or cloud machine, make `.env` readable only by you: `chmod 600 .env`.
4. Set `APP_PASSWORD` (and a long random `APP_SECRET`) before the app is reachable by anyone else.
5. Rotate keys once in a while (Finnhub: dashboard → API key; Tiingo: Account → API).

## Backtest, ranking model and TimesFM

The Backtest & Calibration page shows whether the signals actually work: hit rate and average
return after costs, by catalyst and by confidence bucket, against buying every positive event
(naive baseline) and against buying the S&P 500 over the same days. It is walk-forward (trained
only on the past) and uses SEC filings, earnings surprises and FDA approvals, because free news
plans keep too little history.

```bash
# one-off data download (resumable; Tiingo's free 50 requests/hour makes prices take ~2.5 h)
docker compose run --rm worker python -m catalystedge.backtest.fetch sec
docker compose run --rm worker python -m catalystedge.backtest.fetch prices
docker compose run --rm worker python -m catalystedge.backtest.fetch earnings
docker compose run --rm worker python -m catalystedge.backtest.fetch fda
docker compose run --rm worker python -m catalystedge backtest --timesfm
```
The LightGBM ranking model is switched on **only** if it beats every baseline out of sample with
enough trades and a significance test; otherwise it stays off and the page says so.

**TimesFM** (Google's price-forecast model, v3.0) is **off** by default. Switch it on in Settings
(no restart); choose *Feature only* (nudges confidence) or *Filter* (keeps a signal only if the
3-10 day forecast is positive; removed signals are listed). The first switch-on downloads ~1.3 GB.
Its weights are licensed for non-commercial use, and its training data may overlap the backtest
period, so the page warns you when its backtest numbers could be optimistic.

## API keys

| Key | Needed? | Where |
|---|---|---|
| `FINNHUB_API_KEY` | yes | finnhub.io (free) |
| `TIINGO_API_KEY` | yes | tiingo.com (free plan is enough to start) |
| `SEC_USER_AGENT` | yes (not a secret) | your name and email, as SEC requires |
| `MARKETAUX_API_KEY`, `ALPHAVANTAGE_API_KEY` | optional, more news | marketaux.com, alphavantage.co |
| `RESEND_API_KEY` + `ALERT_EMAIL_TO` | for email alerts | resend.com (free 3,000/month) |
| `OPENFDA_API_KEY`, `STOOQ_API_KEY` | optional | open.fda.gov, stooq.com |

## For developers

```bash
scripts/dev_test_db.sh                         # throwaway Postgres 16 on port 55432
export TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/catalystedge_test
cd backend && uv sync && uv run pytest && uv run ruff check catalystedge tests
cd ../frontend && pnpm install && pnpm lint && pnpm typecheck && pnpm test && pnpm build
python3 scripts/verify_sources.py              # checks your keys against each provider's real limits
python3 scripts/check_secrets.py               # scans files + git history for keys (prints locations only)
cd backend && uv run catalystedge sample-news  # live headlines with ticker, sentiment and event type
```
