# CatalystEdge

CatalystEdge finds **positive, news-driven swing-trade setups** in US stocks and runs a
**$100 paper-trading account** on them. It uses end-of-day data only and never trades
intraday. Every confidence number is labelled **UNCALIBRATED** until backtests or real
outcomes support it.

> **Not financial advice.** Research and paper-trading tool only.

## Documents
- [Architecture, schema, data sources, phased plan](docs/ARCHITECTURE.md)
- [$0 deployment: local Docker → Oracle + Vercel + Neon + Upstash](docs/DEPLOY_FREE.md)

## Quick start (local)
```bash
cp .env.example .env     # add free API keys, see docs/ARCHITECTURE.md §14
docker compose up -d     # first run is slow: builds + model download
open http://localhost:3000
```

## Status
- Phase 0: design ✅
- Phase 1: MVP (news adapters, 48 h window, local sentiment, rule-based signals,
  paper account, outcome tracking, email alerts, on-open refresh, dashboard): in progress
- Phase 2: LightGBM, calibration, EDGAR backtest, SHAP
- Phase 3: observability, hardening, optional TimesFM feature, cost review
