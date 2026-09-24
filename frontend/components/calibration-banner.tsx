"use client";

import useSWR from "swr";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import type { Overview } from "@/lib/types";

export function CalibrationBanner() {
  const { data } = useSWR<Overview>("/api/overview");
  if (!data) return null;
  const c = data.calibration;
  const uncal = c.label === "UNCALIBRATED";
  return (
    <div
      role="status"
      className={
        uncal
          ? "flex gap-3 rounded-lg border border-border bg-warning-bg p-3 text-sm text-warning-fg"
          : "flex gap-3 rounded-lg border border-border bg-card p-3 text-sm"
      }
    >
      {uncal ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden /> : <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-good" aria-hidden />}
      <div className="space-y-1">
        <p>
          <strong>{c.label}.</strong> {c.message}
        </p>
        <p className="opacity-90">
          Evidence: {c.closed_trades}/{c.required_closed_trades} closed paper trades · backtest calibration:{" "}
          {c.backtest_calibration ?? "none yet"} · {c.displayed_signal_outcomes_10d} signals with 10-day outcomes.
          Auto-buy: {data.auto_buy.allowed ? "enabled" : "off"} ({data.auto_buy.reason}).
        </p>
      </div>
    </div>
  );
}
