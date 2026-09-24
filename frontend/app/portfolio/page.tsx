"use client";

import * as React from "react";
import useSWR, { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { pct, title, usd } from "@/lib/format";
import type { Performance, Portfolio, Position } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EquityChart } from "@/components/equity-chart";

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div>
      <p className="text-xs text-subtle">{label}</p>
      <p className="text-lg font-semibold tabular">{value}</p>
      {sub && <p className="text-xs text-subtle">{sub}</p>}
    </div>
  );
}

function WhyThisTrade({ why }: { why: Record<string, unknown> }) {
  const trigger = why.exit_trigger as Record<string, unknown> | undefined;
  const costs = why.costs_bps as { half_spread: number; slippage: number } | undefined;
  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-accent">Why this trade?</summary>
      <div className="mt-2 space-y-1 text-muted">
        {typeof why.reason === "string" && <p>{why.reason}</p>}
        <p>
          Rule {String(why.rule_id ?? "manual")} · confidence {why.confidence != null ? `${Number(why.confidence).toFixed(0)}%` : "–"}{" "}
          ({why.calibrated ? "calibrated" : "UNCALIBRATED"})
        </p>
        {why.entry_gap_pct != null && <p>Opened {pct(Number(why.entry_gap_pct), 2)} vs. the signal price at entry.</p>}
        {costs && <p>Assumed costs: {costs.half_spread} bps half-spread + {costs.slippage} bps slippage per side.</p>}
        {trigger && (
          <p>
            Exit: {title(String(trigger.reason))} on the close of {String(trigger.on_close_of)}
            {trigger.gap_below_stop_pct != null && ` · filled ${pct(Number(trigger.gap_below_stop_pct), 2)} vs. the stop (overnight gap)`}
          </p>
        )}
      </div>
    </details>
  );
}

function OpenTable({ rows, onSell }: { rows: Position[]; onSell: (id: number) => void }) {
  if (rows.length === 0) return <p className="p-4 text-sm text-muted">No open positions.</p>;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Symbol</TH><TH>Entry</TH><TH className="text-right">Shares</TH><TH className="text-right">Cost</TH>
          <TH className="text-right">Value</TH><TH className="text-right">P&amp;L</TH><TH>Stop / target</TH><TH>Time stop</TH><TH />
        </TR>
      </THead>
      <TBody>
        {rows.map((p) => (
          <TR key={p.id}>
            <TD>
              <p className="font-medium">{p.symbol}</p>
              <WhyThisTrade why={p.why} />
            </TD>
            <TD>{p.entry_date}</TD>
            <TD className="text-right">{p.qty.toFixed(4)}</TD>
            <TD className="text-right">{usd(p.cost_basis)}</TD>
            <TD className="text-right">{usd(p.value)}</TD>
            <TD className={`text-right ${(p.unrealized_pnl ?? 0) >= 0 ? "text-good" : "text-critical"}`}>{usd(p.unrealized_pnl)}</TD>
            <TD>{usd(p.stop)} / {usd(p.target)}</TD>
            <TD>{p.time_stop_date}</TD>
            <TD>
              <Button size="sm" variant="outline" onClick={() => onSell(p.id)} title={`Earliest sell: the ${p.can_sell_from} open`}>
                Sell at next open
              </Button>
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function ClosedTable({ rows }: { rows: Position[] }) {
  if (rows.length === 0) return <p className="p-4 text-sm text-muted">No closed trades yet.</p>;
  return (
    <Table>
      <THead>
        <TR><TH>Symbol</TH><TH>Entry</TH><TH>Exit</TH><TH>Reason</TH><TH className="text-right">Cost</TH><TH className="text-right">P&amp;L</TH></TR>
      </THead>
      <TBody>
        {rows.map((p) => (
          <TR key={p.id}>
            <TD><p className="font-medium">{p.symbol}</p><WhyThisTrade why={p.why} /></TD>
            <TD>{p.entry_date}</TD>
            <TD>{p.exit_date}</TD>
            <TD>{p.exit_reason ? title(p.exit_reason) : "–"}</TD>
            <TD className="text-right">{usd(p.cost_basis)}</TD>
            <TD className={`text-right ${(p.realized_pnl ?? 0) >= 0 ? "text-good" : "text-critical"}`}>{usd(p.realized_pnl)}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

export default function PortfolioPage() {
  const { data, error } = useSWR<Portfolio>("/api/portfolio");
  const { data: perf } = useSWR<{ stats: Performance; curve: { date: string; equity: number; spy: number | null }[] }>("/api/performance");
  const { mutate } = useSWRConfig();
  const [msg, setMsg] = React.useState<string | null>(null);

  async function sell(id: number) {
    setMsg(null);
    try {
      const r = await api<{ execute_on: string }>("/api/portfolio/sell", { method: "POST", body: JSON.stringify({ position_id: id }) });
      setMsg(`Sell order placed for the ${r.execute_on} open.`);
      await mutate("/api/portfolio");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Sell failed");
    }
  }

  if (error) return <p className="text-sm text-critical">Could not load the portfolio: {error.message}</p>;
  if (!data) return <p className="text-sm text-muted">Loading portfolio…</p>;
  const s = data.performance;
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Paper portfolio</h1>
        <p className="text-sm text-muted">{data.rules}</p>
      </div>
      <Card>
        <CardContent className="grid grid-cols-2 gap-4 pt-4 sm:grid-cols-4 lg:grid-cols-7">
          <Stat label="Equity" value={usd(s.equity)} sub={`started ${usd(s.start_equity)}`} />
          <Stat label="Return" value={pct(s.total_return_pct, 2)} sub={`S&P 500 ${pct(s.spy_return_pct, 2)}`} />
          <Stat label="Cash" value={usd(data.cash)} />
          <Stat label="Win rate" value={s.win_rate_pct == null ? "–" : `${s.win_rate_pct}%`} sub={`${s.closed_trades} closed trades`} />
          <Stat label="Profit factor" value={s.profit_factor ?? "–"} />
          <Stat label="Sharpe" value={s.sharpe ?? "–"} />
          <Stat label="Max drawdown" value={pct(s.max_drawdown_pct, 1, false)} />
        </CardContent>
      </Card>
      <p className="text-xs text-subtle">{s.note}. Auto-buy: {data.auto_buy.allowed ? "enabled" : "off"} ({data.auto_buy.reason}).</p>
      <Card>
        <CardHeader><CardTitle>Equity vs. S&amp;P 500</CardTitle></CardHeader>
        <CardContent><EquityChart data={perf?.curve ?? []} /></CardContent>
      </Card>
      {msg && <p className="text-sm" role="status">{msg}</p>}
      <Tabs defaultValue="open" className="space-y-3">
        <TabsList>
          <TabsTrigger value="open">Open ({data.open.length})</TabsTrigger>
          <TabsTrigger value="pending">Pending orders ({data.pending_orders.length})</TabsTrigger>
          <TabsTrigger value="closed">Closed ({data.closed.length})</TabsTrigger>
          <TabsTrigger value="options">Buy options ({data.buy_options.length})</TabsTrigger>
        </TabsList>
        <Card>
          <TabsContent value="open"><OpenTable rows={data.open} onSell={sell} /></TabsContent>
          <TabsContent value="closed"><ClosedTable rows={data.closed} /></TabsContent>
          <TabsContent value="pending">
            {data.pending_orders.length === 0 ? <p className="p-4 text-sm text-muted">No pending orders.</p> : (
              <Table>
                <THead><TR><TH>Symbol</TH><TH>Side</TH><TH>Executes at the open of</TH><TH className="text-right">Amount</TH><TH>Origin</TH></TR></THead>
                <TBody>
                  {data.pending_orders.map((o) => (
                    <TR key={o.id}>
                      <TD className="font-medium">{o.symbol}</TD><TD>{o.side}{o.exit_reason ? ` (${title(o.exit_reason)})` : ""}</TD>
                      <TD>{o.execute_on}</TD><TD className="text-right">{o.notional_usd != null ? usd(o.notional_usd) : `${o.qty} sh`}</TD><TD>{o.origin}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </TabsContent>
          <TabsContent value="options">
            {data.buy_options.length === 0 ? <p className="p-4 text-sm text-muted">No buy options considered in the last two weeks.</p> : (
              <Table>
                <THead><TR><TH>Date</TH><TH>Symbol</TH><TH className="text-right">Confidence</TH><TH>Decision</TH><TH>Why</TH></TR></THead>
                <TBody>
                  {data.buy_options.map((b) => (
                    <TR key={`${b.signal_id}-${b.decision_date}`}>
                      <TD>{b.decision_date}</TD>
                      <TD className="font-medium">{b.symbol}</TD>
                      <TD className="text-right">{b.confidence.toFixed(0)}% {!b.calibrated && <span className="text-xs text-subtle">(uncal.)</span>}</TD>
                      <TD><Badge variant={b.decision === "bought" ? "good" : "outline"}>{b.decision}</Badge></TD>
                      <TD>
                        {b.skip_explained.length ? (
                          <ul className="list-inside list-disc text-sm text-muted">{b.skip_explained.map((x) => <li key={x}>{x}</li>)}</ul>
                        ) : "Bought"}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </TabsContent>
        </Card>
      </Tabs>
    </div>
  );
}
