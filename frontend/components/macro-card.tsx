"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type Item = { source: string; title: string; url: string; at: string; kind: string | null };
type Macro = { recent: Item[]; upcoming: Item[]; note: string };

const fmt = (iso: string) => new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

/** Market-wide releases (Fed, BLS) and upcoming dates (FRED). Context only, never a stock catalyst. */
export function MacroCard() {
  const { data } = useSWR<Macro>("/api/macro");
  if (!data || (data.recent.length === 0 && data.upcoming.length === 0)) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Macro context</CardTitle></CardHeader>
      <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
        <div>
          <p className="mb-1 text-xs font-medium text-subtle">Coming up (next 7 days)</p>
          {data.upcoming.length === 0 ? <p className="text-muted">Nothing scheduled.</p> : (
            <ul className="space-y-0.5">{data.upcoming.map((u) => <li key={u.url}>{fmt(u.at)} · {u.title}</li>)}</ul>
          )}
        </div>
        <div>
          <p className="mb-1 text-xs font-medium text-subtle">Last 2 days</p>
          {data.recent.length === 0 ? <p className="text-muted">No releases.</p> : (
            <ul className="space-y-0.5">
              {data.recent.map((r) => (
                <li key={r.url}><a href={r.url} target="_blank" rel="noopener noreferrer" className="hover:underline">{r.title}</a>
                  <span className="text-xs text-subtle"> · {r.source === "bls" ? "BLS" : r.source === "federal_reserve" ? "Federal Reserve" : r.source}</span></li>
              ))}
            </ul>
          )}
        </div>
        <p className="text-xs text-subtle sm:col-span-2">{data.note}</p>
      </CardContent>
    </Card>
  );
}
