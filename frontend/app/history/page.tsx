"use client";

import useSWR from "swr";
import { title, usd } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

type Order = {
  id: number; symbol: string; side: string; origin: string; status: string; decision_date: string; execute_on: string;
  exit_reason: string | null; reject_reason: string | null;
  fill: { date: string; raw_open: number; price: number; qty: number; half_spread_bps: number; slippage_bps: number } | null;
};

export default function HistoryPage() {
  const { data, error } = useSWR<{ orders: Order[] }>("/api/trades");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Trade history</h1>
        <p className="text-sm text-muted">Every paper order and fill. Fills use the official open of the execution day, adjusted for spread and slippage.</p>
      </div>
      {error && <p className="text-sm text-critical">{error.message}</p>}
      <Card>
        {data && data.orders.length === 0 && <p className="p-4 text-sm text-muted">No orders yet.</p>}
        {data && data.orders.length > 0 && (
          <Table>
            <THead>
              <TR><TH>Decided</TH><TH>Executes</TH><TH>Symbol</TH><TH>Side</TH><TH>Origin</TH><TH>Status</TH>
                <TH className="text-right">Open</TH><TH className="text-right">Fill</TH><TH className="text-right">Shares</TH><TH>Costs (bps)</TH></TR>
            </THead>
            <TBody>
              {data.orders.map((o) => (
                <TR key={o.id}>
                  <TD>{o.decision_date}</TD><TD>{o.execute_on}</TD><TD className="font-medium">{o.symbol}</TD>
                  <TD>{o.side}{o.exit_reason ? ` · ${title(o.exit_reason)}` : ""}</TD><TD>{o.origin}</TD>
                  <TD>
                    <Badge variant={o.status === "filled" ? "good" : o.status === "pending" ? "default" : "warning"} title={o.reject_reason ?? undefined}>{o.status}</Badge>
                    {o.reject_reason && <p className="mt-1 text-xs text-subtle">{o.reject_reason}</p>}
                  </TD>
                  <TD className="text-right">{o.fill ? usd(o.fill.raw_open) : "–"}</TD>
                  <TD className="text-right">{o.fill ? usd(o.fill.price) : "–"}</TD>
                  <TD className="text-right">{o.fill ? o.fill.qty.toFixed(4) : "–"}</TD>
                  <TD>{o.fill ? `${o.fill.half_spread_bps} + ${o.fill.slippage_bps}` : "–"}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>
    </div>
  );
}
