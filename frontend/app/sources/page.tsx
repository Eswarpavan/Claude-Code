"use client";

import useSWR from "swr";
import type { SourceRow } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

type Model = { name: string; repo: string; status: string; detail: string };
const HEALTH: Record<string, "good" | "warning" | "critical" | "outline"> = { ok: "good", stale: "warning", failed: "critical", disabled: "outline" };

export default function SourcesPage() {
  const { data, error } = useSWR<{ sources: SourceRow[]; models: Model[]; email: { provider: string | null; recipient_configured: boolean } }>("/api/sources");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Source health</h1>
        <p className="text-sm text-muted">Each data source, when it last worked, and how much of its free daily budget is used. FRAGILE sources are unofficial and only used as fallbacks.</p>
      </div>
      {error && <p className="text-sm text-critical">{error.message}</p>}
      <Card>
        {data && (
          <Table>
            <THead><TR><TH>Source</TH><TH>Health</TH><TH>Last success</TH><TH className="text-right">Items</TH><TH className="text-right">Calls today</TH><TH>Notes</TH></TR></THead>
            <TBody>
              {data.sources.map((s) => (
                <TR key={s.key}>
                  <TD>
                    <p className="font-medium">{s.key.replace(/_/g, " ")}</p>
                    <p className="text-xs text-subtle">{s.kind}{s.fragile ? " · FRAGILE (unofficial)" : ""}{!s.official ? " · unofficial" : ""}</p>
                  </TD>
                  <TD><Badge variant={HEALTH[s.health]}>{s.health}</Badge></TD>
                  <TD className="text-sm">{s.last_success_at ? new Date(s.last_success_at).toLocaleString() : "never"}</TD>
                  <TD className="text-right">{s.items_last_run ?? "–"}</TD>
                  <TD className="text-right">{s.budget_used_today}{s.daily_budget ? ` / ${s.daily_budget}` : ""}</TD>
                  <TD className="max-w-xs text-xs text-muted">{s.last_error ?? s.note}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>
      <div className="grid gap-3 md:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Sentiment models</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {data?.models.map((m) => (
              <div key={m.name} className="flex items-start justify-between gap-2">
                <div><p className="font-medium">{m.name}</p><p className="text-xs text-subtle">{m.repo}</p></div>
                <div className="text-right"><Badge variant={m.status === "ready" ? "good" : m.status === "failed" ? "critical" : "outline"}>{m.status}</Badge><p className="text-xs text-subtle">{m.detail}</p></div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Email alerts</CardTitle></CardHeader>
          <CardContent className="text-sm">
            {data && (data.email.provider && data.email.recipient_configured
              ? <p>Sending via <strong>{data.email.provider}</strong>.</p>
              : <p className="text-muted">Not configured: set RESEND_API_KEY (or SendGrid/SMTP) and ALERT_EMAIL_TO. Alerts wait in the queue until then.</p>)}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
