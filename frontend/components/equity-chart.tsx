"use client";

import * as React from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useThemeColors } from "@/lib/theme";
import { usd } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

type Point = { date: string; equity: number; spy: number | null };

/** Direct label at the last point of a line (selective labelling: one per series, not every point). */
function endLabel(text: string, n: number, fill: string) {
  function EndLabel(p: { index?: number; x?: number | string; y?: number | string }) {
    if (p.index !== n - 1) return <g />;
    return <text x={Number(p.x) + 6} y={Number(p.y)} dy={4} fontSize={11} fill={fill}>{text}</text>;
  }
  return EndLabel;
}

/** Paper account vs. the same $100 in the S&P 500 (SPY), one shared dollar axis. */
export function EquityChart({ data }: { data: Point[] }) {
  const c = useThemeColors();
  const [table, setTable] = React.useState(false);
  if (data.length === 0) {
    return <p className="text-sm text-muted">The equity curve starts after the first daily close with the account open.</p>;
  }
  const last = data[data.length - 1];
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted tabular">
          Now: account {usd(last.equity)} · S&amp;P 500 {usd(last.spy)}
        </p>
        <Button variant="ghost" size="sm" onClick={() => setTable(!table)} aria-pressed={table}>
          {table ? "Show chart" : "Show table"}
        </Button>
      </div>
      {table || !c ? (
        <Table>
          <THead>
            <TR>
              <TH>Date</TH>
              <TH className="text-right">Paper account</TH>
              <TH className="text-right">S&amp;P 500 (SPY)</TH>
            </TR>
          </THead>
          <TBody>
            {data.map((p) => (
              <TR key={p.date}>
                <TD>{p.date}</TD>
                <TD className="text-right">{usd(p.equity)}</TD>
                <TD className="text-right">{usd(p.spy)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <div className="h-64 w-full" role="img" aria-label="Equity curve of the paper account compared with the S&P 500">
          <ResponsiveContainer>
            <LineChart data={data} margin={{ top: 8, right: 56, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={c["--grid"]} vertical={false} />
              <XAxis dataKey="date" stroke={c["--axis"]} tick={{ fill: c["--subtle"], fontSize: 11 }} tickLine={false} minTickGap={24} />
              <YAxis stroke={c["--axis"]} tick={{ fill: c["--subtle"], fontSize: 11 }} tickLine={false} axisLine={false}
                     domain={["auto", "auto"]} tickFormatter={(v: number) => `$${v.toFixed(0)}`} width={48} />
              <Tooltip
                contentStyle={{ background: c["--card"], border: `1px solid ${c["--grid"]}`, borderRadius: 8, color: c["--foreground"] }}
                labelStyle={{ color: c["--muted"] }}
                formatter={(v) => usd(Number(v))}
                cursor={{ stroke: c["--axis"], strokeWidth: 1 }}
              />
              <Legend wrapperStyle={{ color: c["--muted"], fontSize: 12 }} />
              <Line type="monotone" dataKey="equity" name="Paper account" stroke={c["--series-1"]} strokeWidth={2} dot={false}
                    activeDot={{ r: 4, stroke: c["--card"], strokeWidth: 2 }} isAnimationActive={false}
                    label={endLabel("Account", data.length, c["--foreground"])} />
              <Line type="monotone" dataKey="spy" name="S&P 500 (SPY)" stroke={c["--series-2"]} strokeWidth={2} dot={false}
                    activeDot={{ r: 4, stroke: c["--card"], strokeWidth: 2 }} connectNulls isAnimationActive={false}
                    label={endLabel("S&P 500", data.length, c["--foreground"])} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
