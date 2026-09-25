"use client";

import * as React from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, tokenStore } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { TimesFMSettings } from "@/components/timesfm";
import { LLMSettings } from "@/components/llm-settings";

type Paper = { auto_buy: boolean; auto_buy_threshold: number; risk_per_trade: number; max_position_pct: number; max_open_positions: number;
  cash_floor_pct: number; time_stop_sessions: number; start_cash: number };
type SettingsResponse = { paper: Paper; auto_buy_gate: string; auto_buy_effective: boolean };
type LogRow = { id: number; kind: string; symbol: string | null; status: string; attempts: number; provider: string | null; created_at: string | null; sent_at: string | null; last_error: string | null; subject: string | null };

const FIELDS: [keyof Paper, string, number, number, number][] = [
  ["auto_buy_threshold", "Auto-buy confidence threshold (%)", 65, 100, 1],
  ["risk_per_trade", "Risk per trade (fraction of equity)", 0.005, 0.05, 0.005],
  ["max_position_pct", "Max position size (fraction of equity)", 0.05, 0.25, 0.01],
  ["max_open_positions", "Max open positions", 1, 10, 1],
  ["cash_floor_pct", "Cash floor (fraction of equity)", 0, 0.5, 0.05],
  ["time_stop_sessions", "Time stop (trading days)", 2, 30, 1],
];

export default function SettingsPage() {
  const { data, error } = useSWR<SettingsResponse>("/api/settings");
  const { data: log } = useSWR<{ log: LogRow[] }>("/api/notifications");
  const { mutate } = useSWRConfig();
  const [draft, setDraft] = React.useState<Partial<Paper>>({});
  const [msg, setMsg] = React.useState<string | null>(null);

  async function save(patch: Partial<Paper>) {
    setMsg(null);
    try {
      const r = await api<SettingsResponse>("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      await mutate("/api/settings", r, { revalidate: false });
      await mutate("/api/overview");
      setDraft({});
      setMsg("Saved.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Save failed");
    }
  }

  if (error) return <p className="text-sm text-critical">{error.message}</p>;
  if (!data) return <p className="text-sm text-muted">Loading settings…</p>;
  const p = { ...data.paper, ...draft };
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>
      <Card>
        <CardHeader><CardTitle>Auto-buy</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <label className="flex items-center gap-3">
            <Switch checked={p.auto_buy} onCheckedChange={(v) => save({ auto_buy: v })} aria-label="Request auto-buy" />
            <span>Request auto-buy of signals at or above the threshold</span>
          </label>
          <p className={data.auto_buy_effective ? "text-good" : "text-muted"}>
            {data.auto_buy_effective ? "Auto-buy is active." : `Auto-buy is not active: ${data.auto_buy_gate}.`}
          </p>
          <p className="text-xs text-subtle">The server only enables auto-buy once a historical backtest calibration exists or 30 paper trades have closed. The switch alone cannot turn it on.</p>
        </CardContent>
      </Card>
      <TimesFMSettings />
      <LLMSettings />
      <Card>
        <CardHeader><CardTitle>Paper account ({"$"}{p.start_cash} start)</CardTitle></CardHeader>
        <CardContent>
          <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); void save(draft); }}>
            {FIELDS.map(([k, label, min, max, step]) => (
              <label key={k} className="flex flex-col gap-1 text-sm">
                <span className="text-subtle">{label}</span>
                <Input type="number" min={min} max={max} step={step} value={String(p[k])}
                       onChange={(e) => setDraft({ ...draft, [k]: Number(e.target.value) })} />
              </label>
            ))}
            <div className="flex items-center gap-3 sm:col-span-2">
              <Button type="submit" disabled={Object.keys(draft).length === 0}>Save</Button>
              {msg && <span className="text-sm text-muted" role="status">{msg}</span>}
            </div>
          </form>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Email send log</CardTitle></CardHeader>
        <CardContent>
          {!log || log.log.length === 0 ? <p className="text-sm text-muted">No alerts yet.</p> : (
            <Table>
              <THead><TR><TH>Created</TH><TH>Kind</TH><TH>Subject</TH><TH>Status</TH><TH className="text-right">Attempts</TH><TH>Error</TH></TR></THead>
              <TBody>
                {log.log.map((n) => (
                  <TR key={n.id}>
                    <TD className="text-xs">{n.created_at ? new Date(n.created_at).toLocaleString() : "–"}</TD>
                    <TD>{n.kind}</TD><TD>{n.subject}</TD><TD>{n.status}</TD><TD className="text-right">{n.attempts}</TD>
                    <TD className="max-w-xs text-xs text-muted">{n.last_error}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Session</CardTitle></CardHeader>
        <CardContent>
          <Button variant="outline" onClick={() => { tokenStore.set(null); window.dispatchEvent(new Event("catalystedge:login")); }}>Log out</Button>
        </CardContent>
      </Card>
    </div>
  );
}
