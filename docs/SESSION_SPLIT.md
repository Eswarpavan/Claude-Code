# Two Claude sessions are working on this branch

Written by session `session_01NyQk5bNn9ZHppKW2QPHxtf` on 2026-09-24 ~08:15 UTC.
Session `session_018KK5fTC9F39hWBuHr54gpZ` is also pushing to
`claude/adoring-dirac-ohiyp8`. To avoid building the same thing twice:

| Area | Owner |
|---|---|
| News pipeline, ticker linking, classification, non-event filter | 01Ny (done) |
| **Signal engine** `backend/catalystedge/signals/` | 01Ny |
| SEC 8-K / Form 4 / earnings / FDA + trial events into the pipeline | 01Ny |
| LightGBM ranker, calibration, SHAP, walk-forward backtest, TimesFM | 01Ny |
| Prices, paper engine, outcomes, metrics | 018K (done) |
| Alerts/email, on-open refresh, scheduler | 018K |
| FastAPI API + single-password auth, Next.js dashboard, Docker Compose | 018K |

Interface: `catalystedge.signals.engine.generate_signals(session, now)` writes
rows to the existing `signals` table (and returns them); everything else reads
that table.

Rules: pull/merge before every push, never force-push, small commits.
If you (018K) already built part of 01Ny's list, edit this table and push; 01Ny
checks this file before each push and will switch areas.
