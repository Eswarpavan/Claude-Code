"use client";

import * as React from "react";
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useThemeColors } from "@/lib/theme";
import type { Bucket } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/** Reliability view: observed hit rate per confidence bucket vs. what the confidence claimed.
 * Both are percentages on one axis; the dashed neutral line is "perfect calibration". */
export function CalibrationChart({ buckets, horizon }: { buckets: Bucket[]; horizon: number }) {
  const c = useThemeColors();
  const [table, setTable] = React.useState(false);
  const data = buckets.map((b) => {
    const [lo, hi] = b.bucket.split("-").map(Number);
    return { ...b, claimed: (lo + hi) / 2 };
  });
  const total = buckets.reduce((a, b) => a + b.n, 0);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted">
          {horizon}-trading-day outcomes · {total} displayed signals measured
          {total < 30 && " · too few to judge calibration yet"}
        </p>
        <Button variant="ghost" size="sm" onClick={() => setTable(!table)} aria-pressed={table}>
          {table ? "Show chart" : "Show table"}
        </Button>
      </div>
      {table || !c ? (
        <Table>
          <THead>
            <TR>
              <TH>Confidence bucket</TH>
              <TH className="text-right">Signals</TH>
              <TH className="text-right">Hit rate</TH>
              <TH className="text-right">Avg return (net)</TH>
            </TR>
          </THead>
          <TBody>
            {buckets.map((b) => (
              <TR key={b.bucket}>
                <TD>{b.bucket}%</TD>
                <TD className="text-right">{b.n}</TD>
                <TD className="text-right">{b.hit_rate == null ? "–" : `${b.hit_rate}%`}</TD>
                <TD className="text-right">{b.avg_return_pct == null ? "–" : `${b.avg_return_pct}%`}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <div className="h-60 w-full" role="img" aria-label={`Hit rate per confidence bucket, ${horizon}-day horizon`}>
          <ResponsiveContainer>
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }} barCategoryGap={6}>
              <CartesianGrid stroke={c["--grid"]} vertical={false} />
              <XAxis dataKey="bucket" stroke={c["--axis"]} tick={{ fill: c["--subtle"], fontSize: 11 }} tickLine={false} />
              <YAxis domain={[0, 100]} stroke={c["--axis"]} tick={{ fill: c["--subtle"], fontSize: 11 }} tickLine={false}
                     axisLine={false} tickFormatter={(v: number) => `${v}%`} width={40} />
              <Tooltip
                contentStyle={{ background: c["--card"], border: `1px solid ${c["--grid"]}`, borderRadius: 8, color: c["--foreground"] }}
                labelStyle={{ color: c["--muted"] }}
                formatter={(v, name) => [v == null ? "no data" : `${Number(v).toFixed(1)}%`, String(name)]}
                cursor={{ fill: c["--foreground"], fillOpacity: 0.05 }}
              />
              <Legend wrapperStyle={{ color: c["--muted"], fontSize: 12 }} />
              <Bar dataKey="hit_rate" name="Observed hit rate" fill={c["--series-1"]} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              <Line dataKey="claimed" name="Perfect calibration" stroke={c["--muted"]} strokeDasharray="4 4" strokeWidth={2}
                    dot={false} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
