import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import { TimesFMHoverLines, TimesFMIndicator } from "@/components/timesfm";
import { BacktestSummary } from "@/components/evidence";
import type { BacktestReport, TimesFMInfo } from "@/lib/types";

const withData = (data: Record<string, unknown>, ui: React.ReactNode) =>
  render(
    <SWRConfig value={{ provider: () => new Map(), fetcher: (k: string) => Promise.resolve(data[k]), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );

const info = (over: Partial<TimesFMInfo> = {}): TimesFMInfo => ({
  enabled: false, mode: "feature", status: { status: "off" }, changes: [], model: "timesfm-3.0", repo: "google/timesfm-3.0-pytorch",
  license_note: "non-commercial", leakage_note: "may overlap", modes: { feature: "input", filter: "keep if positive" }, ...over,
});

describe("TimesFM hover lines", () => {
  it("renders nothing when TimesFM is off", () => {
    const { container } = render(<TimesFMHoverLines tag={{ enabled: false, mode: null, confidence_delta: 0, filtered: false, note: null, warning: null }} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("shows expected return, range and effect on confidence", () => {
    render(<TimesFMHoverLines tag={{ enabled: true, mode: "feature", confidence_delta: 2.5, filtered: false, note: "n", warning: null,
      forecast: { expected_return_pct: 1.25, low_pct: -2, high_pct: 4, horizon_days: 10, model: "timesfm-3.0", as_of: "2026-09-23" } }} />);
    expect(screen.getByText(/\+1\.25% over 3–10 days · range -2\.0% to \+4\.0% · confidence \+2\.5/)).toBeInTheDocument();
  });
  it("shows the fallback warning", () => {
    render(<TimesFMHoverLines tag={{ enabled: true, mode: "filter", confidence_delta: 0, filtered: false, note: null,
      warning: "TimesFM failed: no forecast for this ticker, normal pipeline result used" }} />);
    expect(screen.getByText(/normal pipeline result used/)).toBeInTheDocument();
  });
});

describe("TimesFM header indicator", () => {
  it("says off by default", async () => {
    withData({ "/api/timesfm": info() }, <TimesFMIndicator />);
    expect(await screen.findByText("TimesFM off")).toBeInTheDocument();
  });
  it("shows mode and status when on", async () => {
    withData({ "/api/timesfm": info({ enabled: true, mode: "filter", status: { status: "ready" } }) }, <TimesFMIndicator />);
    expect(await screen.findByText("TimesFM filter · ready")).toBeInTheDocument();
  });
});

describe("Backtest summary", () => {
  const s = (n: number, h: number, m: number, sh: number) => ({ n, hit_rate: h, mean_pct: m, sharpe: sh });
  const report: BacktestReport = {
    period: ["2025-01-02", "2026-09-01"],
    strategies: { rules: s(120, 0.55, 0.8, 1.1), model: s(90, 0.53, 0.5, 0.7), all_events: s(400, 0.5, 0.2, 0.3), spy_same_days: s(400, 0.56, 0.3, 0.9) },
    verdict: { model_enabled: false, model_beats_baselines: false, rules_beat_baselines: false, plain: "The ranking model did NOT beat all baselines, so it stays OFF (rules only)." },
    by_catalyst: { all_events: {}, rules: {} },
    confidence_buckets: { rules: [{ bucket: "65.0-80.0", ...s(80, 0.54, 0.7, 1.0) }], model: [] },
    caveats: ["Survivorship bias"],
    timesfm: { helps: false, plain: "The evidence says TimesFM is NOT helping here", leakage_warning: "may overlap pretraining",
      rules_without_timesfm: s(100, 0.55, 0.8, 1.1), rules_with_timesfm_filter: s(60, 0.52, 0.4, 0.5),
      naive_all_events: s(400, 0.5, 0.2, 0.3), spy_same_days: s(400, 0.56, 0.3, 0.9) },
  };
  it("shows baselines, model OFF verdict and the TimesFM warning", async () => {
    withData({ "/api/backtest/latest": { run: { id: 1, finished_at: "2026-09-24" }, report } }, <BacktestSummary />);
    expect(await screen.findByText("Model OFF")).toBeInTheDocument();
    expect(screen.getByText("Buy the S&P 500 (same days)")).toBeInTheDocument();
    expect(screen.getByText("Naive: buy every positive event")).toBeInTheDocument();
    expect(screen.getByText(/TimesFM is NOT helping/)).toBeInTheDocument();
    expect(screen.getByText("may overlap pretraining")).toBeInTheDocument();
  });
});
