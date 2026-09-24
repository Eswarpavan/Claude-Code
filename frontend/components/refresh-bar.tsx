"use client";

import * as React from "react";
import { useSWRConfig } from "swr";
import { RefreshCw } from "lucide-react";
import { api, streamEvents } from "@/lib/api";
import type { RefreshState } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";

const STATUS_VARIANT: Record<string, "good" | "warning" | "critical" | "outline"> = {
  ok: "good",
  disabled: "outline",
  budget: "warning",
  quota: "warning",
  circuit: "warning",
  failed: "critical",
};

/**
 * On-open refresh: cached data is already on screen (SWR); this starts a background refresh
 * (the API enforces a cooldown so repeated opens do not burn free quotas) and streams progress.
 */
export function RefreshBar() {
  const { mutate } = useSWRConfig();
  const [state, setState] = React.useState<RefreshState | null>(null);
  const [note, setNote] = React.useState<string | null>(null);
  const started = React.useRef(false);

  const run = React.useCallback(
    async (trigger: "open" | "manual") => {
      setNote(null);
      try {
        const r = await api<{ refresh_id: string | null; started: boolean; cooldown_s: number }>(
          `/api/refresh?trigger=${trigger}`,
          { method: "POST" },
        );
        if (!r.refresh_id) return;
        if (!r.started) setNote(`Refreshed recently; next refresh allowed within ${Math.round(r.cooldown_s / 60)} min.`);
        const ctrl = new AbortController();
        await streamEvents(`/api/refresh/${r.refresh_id}/events`, (d) => setState(d as RefreshState), ctrl.signal);
        await mutate(() => true);
      } catch (e) {
        setNote(e instanceof Error ? `Refresh unavailable: ${e.message}` : "Refresh unavailable");
      }
    },
    [mutate],
  );

  React.useEffect(() => {
    if (started.current) return;
    started.current = true;
    void run("open");
  }, [run]);

  const r = state?.refresh;
  const running = r?.status === "running";
  return (
    <div className="space-y-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm">
            {running ? "Refreshing news, events, prices and signals…" : r ? `Last refresh: ${r.status}` : "Showing cached data"}
            {r?.finished_at && <span className="text-subtle"> · {new Date(r.finished_at).toLocaleString()}</span>}
          </p>
          {note && <p className="text-xs text-subtle">{note}</p>}
        </div>
        <Button variant="outline" size="sm" onClick={() => run("manual")} disabled={running}>
          <RefreshCw className={running ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} aria-hidden /> Refresh now
        </Button>
      </div>
      {r && <Progress value={r.progress_pct} aria-label="Refresh progress" />}
      {state?.sources && state.sources.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Per-source status">
          {state.sources.map((s) => (
            <li key={`${s.key}-${s.at}`}>
              <Badge variant={STATUS_VARIANT[s.status] ?? "default"} title={s.error ?? undefined}>
                {s.key.replace(/_/g, " ")}: {s.status} {s.items ? `· ${s.items}` : ""}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
