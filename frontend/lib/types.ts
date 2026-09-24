export type Headline = { headline: string; url: string; source: string | null; at: string; event_type: string; strength: string | null };

export type SignalT = {
  id: number;
  symbol: string;
  company: string;
  sector: string | null;
  market_cap: number | null;
  as_of_date: string;
  catalyst: string;
  rule_id: string;
  confidence: number;
  calibrated: boolean;
  confidence_label: "CALIBRATED" | "UNCALIBRATED";
  highlight: boolean;
  expected_return_pct: number;
  expected_return_basis: string;
  holding_days: [number, number];
  entry_ref_price: number;
  stop_price: number;
  target_price: number;
  suggested_size_usd: number;
  risk_notes: string[];
  reason: string;
  news_at: string | null;
  hours_since_news: number | null;
  headlines: Headline[];
  sentiment: { pos: number; neu: number; neg: number } | null;
  sentiment_model: string | null;
  model_disagrees: boolean;
  features?: Record<string, unknown>;
  shap?: { base: number; top: { feature: string; contribution: number; plain: string }[] } | null;
  timesfm?: TimesFMTag | null;
  rule_components?: Record<string, number> | null;
  reaction_since_news_pct?: number | null;
};

export type CalibrationStatus = {
  label: "CALIBRATED" | "UNCALIBRATED";
  message: string;
  closed_trades: number;
  required_closed_trades: number;
  backtest_calibration: string | null;
  displayed_signal_outcomes_10d: number;
};

export type Overview = {
  disclaimer: string;
  data_mode: string;
  calibration: CalibrationStatus;
  auto_buy: { allowed: boolean; reason: string; closed_trades: number; required_closed_trades: number };
  last_refresh: { id: string; status: string; started_at: string; progress_pct: number } | null;
  as_of_session: string;
};

export type Position = {
  id: number;
  symbol: string;
  entry_date: string;
  qty: number;
  cost_basis: number;
  stop: number;
  target: number;
  time_stop_date: string;
  last_close: number | null;
  value: number | null;
  unrealized_pnl: number | null;
  status: "open" | "closed";
  exit_date: string | null;
  exit_reason: string | null;
  realized_pnl: number | null;
  why: Record<string, unknown>;
  can_sell_from: string;
};

export type Performance = {
  start_equity: number;
  equity: number;
  total_return_pct: number;
  spy_return_pct: number | null;
  closed_trades: number;
  win_rate_pct: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  profit_factor: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  days: number;
  note: string;
};

export type Portfolio = {
  cash: number;
  start_cash: number;
  performance: Performance;
  auto_buy: { allowed: boolean; reason: string };
  open: Position[];
  closed: Position[];
  pending_orders: { id: number; symbol: string; side: string; execute_on: string; notional_usd: number | null; qty: number | null; origin: string; exit_reason: string | null }[];
  buy_options: { decision_date: string; symbol: string; signal_id: number; confidence: number; calibrated: boolean; decision: string; skip_reasons: string[]; skip_explained: string[]; details: Record<string, unknown> }[];
  rules: string;
};

export type Bucket = { bucket: string; n: number; avg_confidence: number | null; hit_rate: number | null; avg_return_pct: number | null };

export type SourceRow = {
  key: string;
  kind: string;
  official: boolean;
  fragile: boolean;
  health: "ok" | "stale" | "failed" | "disabled";
  status: string;
  last_success_at: string | null;
  last_error: string | null;
  budget_used_today: number;
  daily_budget: number | null;
  items_last_run: number | null;
  note: string;
};

export type RefreshState = {
  refresh: { id: string; trigger: string; status: string; progress_pct: number; started_at: string; finished_at: string | null } | null;
  sources?: { key: string; status: string; items: number; calls: number; error: string | null; at: string | null }[];
};

// ----------------------------------------------------------------------------- TimesFM + evidence (signal brain)

export type TimesFMForecast = { expected_return_pct: number; low_pct: number; high_pct: number; horizon_days: number; model: string; as_of: string };
export type TimesFMTag = {
  enabled: boolean;
  mode: "feature" | "filter" | null;
  confidence_delta: number;
  filtered: boolean;
  note: string | null;
  warning: string | null;
  forecast?: TimesFMForecast;
};
export type TimesFMInfo = {
  enabled: boolean;
  mode: "feature" | "filter";
  status: { status: string; detail?: string; updated_at?: string };
  changes: { at: string; enabled: boolean; mode: string; by: string }[];
  model: string;
  repo: string;
  license_note: string;
  leakage_note: string;
  modes: Record<"feature" | "filter", string>;
};
export type Stat = { n: number; hit_rate: number | null; mean_pct: number | null; median_pct?: number | null; sharpe: number | null };
export type BacktestReport = {
  period: [string, string] | null;
  strategies: Record<"all_events" | "rules" | "model" | "spy_same_days", Stat>;
  verdict: { model_enabled: boolean; model_beats_baselines: boolean; rules_beat_baselines: boolean; plain: string };
  by_catalyst: { all_events: Record<string, Stat>; rules: Record<string, Stat> };
  confidence_buckets: { rules: (Stat & { bucket: string })[]; model: (Stat & { bucket: string })[] };
  priced_in_skip?: { skipped: Stat; kept: Stat };
  caveats: string[];
  timesfm?: {
    helps: boolean;
    plain: string;
    leakage_warning: string | null;
    rules_without_timesfm: Stat;
    rules_with_timesfm_filter: Stat;
    rules_with_timesfm_feature?: Stat;
    filter_helps?: boolean;
    feature_helps?: boolean;
    naive_all_events: Stat;
    spy_same_days: Stat;
  };
};
export type CatalystReport = {
  live: Record<string, Record<string, Stat & { mean_excess_vs_spy_pct?: number | null }>>;
  backtest: { run_id: number | null; period: [string, string] | null; rules: Record<string, Stat>; all_events: Record<string, Stat> };
  evidence: { plain: string; label: string };
};
