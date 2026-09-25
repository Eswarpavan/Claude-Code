import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import TopSignalsPage from "@/app/top/page";
import type { TopSignalsResponse } from "@/lib/types";

const base = { spy_win_rate: 0.5, spy_avg_return_pct: 0.4, sharpe: 1, source: "backtest of 2026-09-25", status: "enabled" };
const data: TopSignalsResponse = {
  as_of_date: "2026-09-24",
  note: "No profit is promised.",
  signals: [
    { id: 1, symbol: "ABC", company: "ABC Corp", catalyst: "insider_buy_cluster", confidence: 78, confidence_label: "UNCALIBRATED",
      headline: { headline: "Directors buy ABC shares", url: "https://example.com/a", source: "sec", at: "2026-09-24T20:00:00Z" },
      expected: { prior_return_pct: 2.5, basis: "prior", stop_pct: -5, target_pct: 6, holding_days: [5, 10] },
      history: { ...base, catalyst: "insider_buy_cluster", label: "insider buying", n: 52, win_rate: 0.56, avg_return_pct: 0.9, enough_data: true, text: "" } },
    { id: 2, symbol: "XYZ", company: "XYZ Inc", catalyst: "contract_win", confidence: 70, confidence_label: "UNCALIBRATED",
      headline: null,
      expected: { prior_return_pct: 2, basis: "prior", stop_pct: -4, target_pct: 5, holding_days: [3, 10] },
      history: { ...base, catalyst: "contract_win", label: "contract win", n: 0, win_rate: null, avg_return_pct: null, enough_data: false,
        text: "Contract win: not enough data yet (not backtestable without a news archive)." } },
  ],
};

describe("Top signals", () => {
  it("shows each signal next to its catalyst's real history, and never a promised profit", () => {
    render(<SWRConfig value={{ provider: () => new Map(), fallback: { "/api/signals/top": data } }}><TopSignalsPage /></SWRConfig>);
    expect(screen.getByText("Directors buy ABC shares")).toBeTruthy();
    expect(screen.getByText("+0.90%")).toBeTruthy();
    expect(screen.getByText("56%")).toBeTruthy();
    expect(screen.getByText(/not enough data yet/)).toBeTruthy();
    expect(screen.getAllByText(/not a backtest result or a promise/).length).toBe(2);
    expect(document.body.textContent).not.toMatch(/guarantee|minimum profit/i);
  });
});

describe("Top signals confidence warning", () => {
  it("says plainly when the backtest shows confidence ranks backwards", () => {
    const bad = { ...data, confidence_check: { reliable: false, worse: ["80+ vs 65-80"], text: "Higher-confidence signals did worse." } };
    render(<SWRConfig value={{ provider: () => new Map(), fallback: { "/api/signals/top": bad } }}><TopSignalsPage /></SWRConfig>);
    expect(screen.getByRole("alert").textContent).toMatch(/not a reliable ranking/);
    expect(screen.getAllByText("does not rank trades (backtest)").length).toBe(2);
  });
});
