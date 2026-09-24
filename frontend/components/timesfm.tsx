"use client";

import * as React from "react";
import Link from "next/link";
import useSWR, { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { pct, title } from "@/lib/format";
import type { TimesFMInfo, TimesFMTag } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

const STATUS_VARIANT: Record<string, "good" | "warning" | "critical" | "outline" | "accent"> = {
  ready: "good",
  running: "accent",
  "model missing": "warning",
  failed: "critical",
  off: "outline",
};

/** Settings card: on/off, mode (only when on), status, notes and the change log. Saves to the database. */
export function TimesFMSettings() {
  const { data, error } = useSWR<TimesFMInfo>("/api/timesfm", { refreshInterval: 10_000 });
  const { mutate } = useSWRConfig();
  const [msg, setMsg] = React.useState<string | null>(null);

  async function save(enabled: boolean, mode?: "feature" | "filter") {
    setMsg(null);
    try {
      const r = await api<TimesFMInfo & { refresh: string }>("/api/timesfm", {
        method: "PUT",
        body: JSON.stringify({ enabled, mode: mode ?? data?.mode }),
      });
      await mutate("/api/timesfm", r, { revalidate: false });
      await mutate("/api/signals");
      setMsg(enabled ? "Saved. Forecasts are being refreshed in the background." : "Saved. TimesFM is off.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Save failed");
    }
  }

  if (error) return <p className="text-sm text-critical">{error.message}</p>;
  if (!data) return null;
  const st = data.status?.status ?? "off";
  return (
    <Card>
      <CardHeader>
        <CardTitle>TimesFM forecast (optional)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <label className="flex items-center gap-3">
          <Switch checked={data.enabled} onCheckedChange={(v) => save(v)} aria-label="Use TimesFM" />
          <span>Use Google&apos;s TimesFM price forecast ({data.model}) on tickers that already have a news signal</span>
        </label>
        {data.enabled && (
          <label className="flex flex-wrap items-center gap-3">
            <span className="text-subtle">Mode</span>
            <Select value={data.mode} onChange={(e) => save(true, e.target.value as "feature" | "filter")} aria-label="TimesFM mode">
              <option value="feature">Feature only</option>
              <option value="filter">Filter</option>
            </Select>
            <span className="text-xs text-muted">{data.modes[data.mode]}</span>
          </label>
        )}
        <p className="flex flex-wrap items-center gap-2">
          <span className="text-subtle">Status</span>
          <Badge variant={STATUS_VARIANT[st] ?? "outline"}>{st}</Badge>
          <span className="text-xs text-muted">{data.status?.detail}</span>
        </p>
        {data.enabled && (st === "failed" || st === "model missing") && (
          <p className="text-warning-fg">TimesFM is not available right now, so signals use the normal pipeline.</p>
        )}
        <p className="text-xs text-subtle">{data.license_note}</p>
        <p className="text-xs text-subtle">{data.leakage_note}</p>
        {msg && <p className="text-muted" role="status">{msg}</p>}
        {data.changes.length > 0 && (
          <details className="text-xs text-muted">
            <summary className="cursor-pointer">Change log ({data.changes.length})</summary>
            <ul className="mt-1 space-y-0.5">
              {data.changes.map((c) => (
                <li key={c.at}>
                  {new Date(c.at).toLocaleString()}: {c.enabled ? `on (${c.mode})` : "off"} by {c.by}
                </li>
              ))}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

/** Small header indicator; links to Settings. */
export function TimesFMIndicator() {
  const { data } = useSWR<TimesFMInfo>("/api/timesfm", { refreshInterval: 30_000 });
  if (!data) return null;
  const st = data.status?.status ?? "off";
  const label = data.enabled ? `TimesFM ${data.mode === "filter" ? "filter" : "feature"} · ${st}` : "TimesFM off";
  return (
    <Link href="/settings" className="hidden shrink-0 sm:block" title="TimesFM setting (click to change)">
      <Badge variant={data.enabled ? STATUS_VARIANT[st] ?? "accent" : "outline"}>{label}</Badge>
    </Link>
  );
}

/** Hover-card lines: TimesFM's expected return, range, and effect on confidence. */
export function TimesFMHoverLines({ tag }: { tag: TimesFMTag | null | undefined }) {
  if (!tag || !tag.enabled) return null;
  const f = tag.forecast;
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-subtle">TimesFM ({tag.mode})</p>
      {f ? (
        <p className="text-sm tabular">
          {pct(f.expected_return_pct, 2)} over 3–{f.horizon_days} days · range {pct(f.low_pct, 1)} to {pct(f.high_pct, 1)}
          {tag.confidence_delta ? ` · confidence ${tag.confidence_delta > 0 ? "+" : ""}${tag.confidence_delta.toFixed(1)}` : ""}
        </p>
      ) : null}
      {tag.warning && <p className="text-xs text-warning-fg">{tag.warning}</p>}
      {tag.note && !tag.warning && <p className="text-xs text-muted">{tag.note}</p>}
    </div>
  );
}

type FilteredRow = { id: number; symbol: string; company: string; catalyst: string; confidence: number; reason: string | null };

/** "Filtered by TimesFM" list (filter mode only). */
export function FilteredByTimesFM() {
  const { data: tf } = useSWR<TimesFMInfo>("/api/timesfm");
  const { data } = useSWR<{ as_of: string | null; signals: FilteredRow[] }>(
    tf?.enabled && tf.mode === "filter" ? "/api/timesfm/filtered" : null,
  );
  if (!tf?.enabled || tf.mode !== "filter" || !data) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Filtered by TimesFM ({data.signals.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {data.signals.length === 0 ? (
          <p className="text-sm text-muted">TimesFM removed no signals{data.as_of ? ` on ${data.as_of}` : ""}.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {data.signals.map((s) => (
              <li key={s.id}>
                <span className="font-medium">{s.symbol}</span> · {s.company} · {title(s.catalyst)} · {s.confidence.toFixed(0)}%
                <span className="block text-xs text-muted">{s.reason}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

