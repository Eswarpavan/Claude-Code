"use client";

import * as React from "react";
import useSWR from "swr";
import type { Bucket, CalibrationStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CalibrationChart } from "@/components/calibration-chart";
import { CalibrationBanner } from "@/components/calibration-banner";
import { BacktestSummary, CatalystTable } from "@/components/evidence";
import { FillCheckCard } from "@/components/fill-check";

type Snapshot = { id: number; created_at: string | null; basis: string; event_family: string; model_version: string | null; n: number;
  buckets: Bucket[] | unknown; brier: number | null; ece: number | null; verdict: string };
type Backtest = { id: number; started_at: string; status: string; sources: string[]; families: string[]; report: Record<string, unknown> | null };
type CalResponse = { status: CalibrationStatus; live_buckets: Record<string, Bucket[]>; snapshots: Snapshot[]; backtests: Backtest[]; news_backtest_note: string };

const VERDICT: Record<string, "good" | "warning" | "critical" | "outline"> = { good: "good", fair: "warning", poor: "critical", insufficient: "outline" };

function ReportView({ report }: { report: Record<string, unknown> }) {
  // Backtest reports come from the backtest module; show the headline comparison fields if present, and the rest as data.
  const keys = ["hit_rate_pct", "mean_return_pct", "sharpe", "max_drawdown_pct", "spy_return_pct", "naive_baseline_return_pct", "trades", "verdict", "model_enabled"];
  const shown = keys.filter((k) => k in report);
  return (
    <div className="space-y-2">
      {shown.length > 0 && (
        <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3 tabular">
          {shown.map((k) => (
            <div key={k}><dt className="text-xs text-subtle">{k.replace(/_/g, " ")}</dt><dd className="font-medium">{String(report[k])}</dd></div>
          ))}
        </dl>
      )}
      <details className="text-xs text-muted">
        <summary className="cursor-pointer">Full report</summary>
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap">{JSON.stringify(report, null, 2)}</pre>
      </details>
    </div>
  );
}

export default function BacktestPage() {
  const { data, error } = useSWR<CalResponse>("/api/calibration");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Backtest &amp; calibration</h1>
        <p className="text-sm text-muted">Does a confidence of 80% really mean 80% of such signals went up? This page shows the evidence.</p>
      </div>
      <CalibrationBanner />
      <BacktestSummary />
      <CatalystTable />
      <FillCheckCard />
      {error && <p className="text-sm text-critical">{error.message}</p>}
      {data && (
        <>
          <Card>
            <CardHeader><CardTitle>Live outcomes by confidence bucket</CardTitle></CardHeader>
            <CardContent>
              <Tabs defaultValue="10" className="space-y-3">
                <TabsList>
                  {["1", "3", "10"].map((h) => <TabsTrigger key={h} value={h}>{h}-day</TabsTrigger>)}
                </TabsList>
                {["1", "3", "10"].map((h) => (
                  <TabsContent key={h} value={h}><CalibrationChart buckets={data.live_buckets[h] ?? []} horizon={Number(h)} /></TabsContent>
                ))}
              </Tabs>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Calibration snapshots</CardTitle></CardHeader>
            <CardContent>
              {data.snapshots.length === 0 ? <p className="text-sm text-muted">No calibration snapshot yet, so every confidence stays UNCALIBRATED.</p> : (
                <ul className="space-y-2 text-sm">
                  {data.snapshots.map((s) => (
                    <li key={s.id} className="flex flex-wrap items-center gap-2">
                      <Badge variant={VERDICT[s.verdict] ?? "outline"}>{s.verdict}</Badge>
                      <span>{s.basis} · {s.event_family} · n={s.n}</span>
                      <span className="text-subtle tabular">Brier {s.brier?.toFixed(3) ?? "–"} · ECE {s.ece?.toFixed(3) ?? "–"}</span>
                      {s.verdict === "poor" && <span className="text-critical">Calibration is poor: treat confidence as a ranking only.</span>}
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Historical backtests (SEC filings &amp; earnings)</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs text-subtle">{data.news_backtest_note}</p>
              {data.backtests.length === 0 ? <p className="text-sm text-muted">No backtest has been run yet.</p> : data.backtests.map((b) => (
                <div key={b.id} className="space-y-1 border-t border-border pt-3 first:border-0 first:pt-0">
                  <p className="text-sm">
                    <Badge variant={b.status === "done" ? "good" : "outline"}>{b.status}</Badge>{" "}
                    {new Date(b.started_at).toLocaleString()} · {b.families.join(", ")} · data: {b.sources.join(", ")}
                  </p>
                  {b.report && <ReportView report={b.report} />}
                </div>
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
