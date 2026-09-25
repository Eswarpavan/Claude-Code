"use client";

import * as React from "react";
import useSWR from "swr";
import { CalibrationBanner } from "@/components/calibration-banner";
import { FilteredByTimesFM } from "@/components/timesfm";
import { MacroCard } from "@/components/macro-card";
import { RefreshBar } from "@/components/refresh-bar";
import { SignalCard } from "@/components/signal-card";
import { Select } from "@/components/ui/input";
import { title } from "@/lib/format";
import type { SignalT } from "@/lib/types";

type SignalsResponse = { as_of_date: string | null; signals: SignalT[]; filters: { sectors: string[]; catalysts: string[] }; note: string };

const CAPS: Record<string, [number | null, number | null]> = {
  any: [null, null],
  large: [10e9, null],
  mid: [2e9, 10e9],
  small: [300e6, 2e9],
  micro: [null, 300e6],
};

export default function SignalsPage() {
  const [sector, setSector] = React.useState("");
  const [catalyst, setCatalyst] = React.useState("");
  const [cap, setCap] = React.useState("any");
  const [minConf, setMinConf] = React.useState(65);

  const qs = new URLSearchParams({ min_confidence: String(minConf) });
  if (sector) qs.set("sector", sector);
  if (catalyst) qs.set("catalyst", catalyst);
  const [lo, hi] = CAPS[cap];
  if (lo != null) qs.set("cap_min", String(lo));
  if (hi != null) qs.set("cap_max", String(hi));
  const { data, error, isLoading } = useSWR<SignalsResponse>(`/api/signals?${qs}`);
  const { data: all } = useSWR<SignalsResponse>("/api/signals");   // filter options from the unfiltered list

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Positive signals</h1>
          <p className="text-sm text-muted">
            Ranked by confidence, then expected return. Only positive, news-driven signals with confidence ≥ 65% are shown;
            ≥ 80% is highlighted. {data?.as_of_date && <>Based on the close of {data.as_of_date}.</>}
          </p>
        </div>
      </div>
      <CalibrationBanner />
      <RefreshBar />

      <div className="flex flex-wrap items-end gap-3" role="group" aria-label="Filters">
        <label className="flex flex-col gap-1 text-xs text-subtle">
          Sector
          <Select value={sector} onChange={(e) => setSector(e.target.value)}>
            <option value="">All sectors</option>
            {all?.filters.sectors.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-subtle">
          Market cap
          <Select value={cap} onChange={(e) => setCap(e.target.value)}>
            <option value="any">Any</option>
            <option value="large">Large (≥ $10B)</option>
            <option value="mid">Mid ($2–10B)</option>
            <option value="small">Small ($300M–2B)</option>
            <option value="micro">Micro (&lt; $300M)</option>
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-subtle">
          Catalyst
          <Select value={catalyst} onChange={(e) => setCatalyst(e.target.value)}>
            <option value="">All catalysts</option>
            {all?.filters.catalysts.map((c) => (
              <option key={c} value={c}>
                {title(c)}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-subtle">
          Min confidence: {minConf}%
          <input type="range" min={65} max={100} step={1} value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} className="w-40 accent-[var(--accent)]" />
        </label>
      </div>

      {error && <p className="text-sm text-critical">Could not load signals: {error.message}</p>}
      {isLoading && !data && <p className="text-sm text-muted">Loading signals…</p>}
      {data && data.signals.length === 0 && (
        <div className="rounded-lg border border-border bg-card p-6 text-sm text-muted">
          No positive signals meet the filters right now. Signals appear after the daily close once a fresh positive catalyst
          (news or filing) is confirmed by price data.
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data?.signals.map((s, i) => <SignalCard key={s.id} signal={s} rank={i + 1} />)}
      </div>
      <FilteredByTimesFM />
      <MacroCard />
    </div>
  );
}
