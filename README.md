# CatalystEdge

CatalystEdge finds **positive, news-driven swing-trade setups** in US stocks and runs a
**$100 paper-trading account** on them. It uses end-of-day data only and never trades
intraday. Every confidence number is labelled **UNCALIBRATED** until backtests or real
outcomes support it.

> **Not financial advice.** Research and paper-trading tool only.

## Documents
- [Architecture, schema, data sources, phased plan](docs/ARCHITECTURE.md)
- [$0 deployment: local Docker → Oracle + Vercel + Neon + Upstash](docs/DEPLOY_FREE.md)

## Step 0: check the real API limits with your keys
Run this before anything else. It uses only the Python standard library and makes about
20 requests in total: at most 3 of Alpha Vantage's 25/day, 2 of Marketaux's 100/day, and
it never sends email.
```bash
cp .env.example .env        # or create .env with just the keys below
python3 scripts/verify_sources.py            # writes reports/source_check_<time>.md/.json
python3 scripts/test_verify_sources.py       # offline tests for the checker itself
```
Keys it reads: `FINNHUB_API_KEY`, `MARKETAUX_API_KEY`, `ALPHAVANTAGE_API_KEY`,
`TIINGO_API_KEY`, `FRED_API_KEY`, `SEC_USER_AGENT` ("Your Name you@example.com"), and
optionally `OPENFDA_API_KEY`, `STOOQ_API_KEY`, `RESEND_API_KEY`. Missing keys are skipped.
Keys are redacted from all output. The report marks each ⚠ item from the docs as
confirmed / differs / unknown.

## Quick start (local)
```bash
cp .env.example .env     # add free API keys, see docs/ARCHITECTURE.md §14
docker compose up -d     # first run is slow: builds + model download
open http://localhost:3000
```

## Status
- Phase 0: design ✅ · source checker ✅ (awaiting a run with real keys)
- Phase 1: MVP (news adapters, 48 h window, local sentiment, rule-based signals,
  paper account, outcome tracking, email alerts, on-open refresh, dashboard): in progress
- Phase 2: LightGBM, calibration, EDGAR backtest, SHAP
- Phase 3: observability, hardening, optional TimesFM feature, cost review
