import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import { SignalCard } from "@/components/signal-card";
import { EquityChart } from "@/components/equity-chart";
import type { SignalT } from "@/lib/types";

const base: SignalT = {
  id: 1, symbol: "MSFT", company: "Microsoft Corp", sector: "Technology", market_cap: 3e12, as_of_date: "2026-09-23",
  catalyst: "upgrade", rule_id: "R_UPGRADE", confidence: 82, calibrated: false, confidence_label: "UNCALIBRATED",
  highlight: true, expected_return_pct: 2.4, expected_return_basis: "prior", holding_days: [3, 10], entry_ref_price: 500,
  stop_price: 470, target_price: 520, suggested_size_usd: 25, risk_notes: ["Large cap: smaller moves"], reason: "Stifel upgrade",
  news_at: "2026-09-23T14:00:00Z", hours_since_news: 5, sentiment: { pos: 0.8, neu: 0.15, neg: 0.05 }, sentiment_model: "finbert",
  model_disagrees: false,
  headlines: [{ headline: "Stifel Upgrades Microsoft to Buy", url: "https://example.com/a", source: "finnhub_news", at: "2026-09-23T14:00:00Z", event_type: "upgrade", strength: "normal" }],
};

const wrap = (ui: React.ReactNode) => render(<SWRConfig value={{ provider: () => new Map() }}>{ui}</SWRConfig>);

describe("SignalCard", () => {
  it("shows every required field and the UNCALIBRATED label", () => {
    wrap(<SignalCard signal={base} rank={1} />);
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("Microsoft Corp")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
    expect(screen.getByText("UNCALIBRATED")).toBeInTheDocument();
    expect(screen.getByText("+2.4%")).toBeInTheDocument();          // expected upside
    expect(screen.getByText("$25.00")).toBeInTheDocument();         // suggested size
    expect(screen.getByText("$470.00")).toBeInTheDocument();        // stop-loss
    expect(screen.getByText("Analyst upgrade")).toBeInTheDocument(); // catalyst badge
    expect(screen.getByText("News 5.0 h ago")).toBeInTheDocument(); // time since news
    expect(screen.getByRole("button", { name: /paper-buy msft at the next open/i })).toBeInTheDocument();
  });

  it("highlights only at 80% and above", () => {
    const { container, rerender } = wrap(<SignalCard signal={base} rank={1} />);
    expect(container.querySelector(".ring-2")).not.toBeNull();
    rerender(<SWRConfig value={{ provider: () => new Map() }}><SignalCard signal={{ ...base, confidence: 70, highlight: false }} rank={1} /></SWRConfig>);
    expect(container.querySelector(".ring-2")).toBeNull();
  });
});


describe("EquityChart", () => {
  it("waits for two trading days before drawing a curve", () => {
    render(<EquityChart data={[{ date: "2026-09-23", equity: 100, spy: null }]} />);
    expect(screen.getByText(/appears after two trading days/)).toBeInTheDocument();
  });
});
