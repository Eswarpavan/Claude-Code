"use client";

import useSWR from "swr";
import { ExternalLink } from "lucide-react";
import { title } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";

type Item = { id: number; symbol: string; headline: string; url: string; source: string | null; at: string; event_type: string;
  strength: string | null; sentiment: { model: string; pos: number; neg: number } | null; reasons: string[] };

export default function NewsPage() {
  const { data, error } = useSWR<{ window_hours: number; items: Item[] }>("/api/news");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">News feed</h1>
        <p className="text-sm text-muted">
          Positive catalysts from the last {data?.window_hours ?? 48} hours only. Older news is dropped automatically; neutral and
          negative headlines are never shown. Links go to the original publisher.
        </p>
      </div>
      {error && <p className="text-sm text-critical">{error.message}</p>}
      <Card className="divide-y divide-border">
        {data?.items.length === 0 && <p className="p-4 text-sm text-muted">No positive catalysts in the window yet.</p>}
        {data?.items.map((n) => (
          <article key={n.id} className="flex flex-col gap-1 p-3 sm:flex-row sm:items-start sm:gap-3">
            <div className="w-28 shrink-0 text-xs text-subtle tabular">{new Date(n.at).toLocaleString()}</div>
            <div className="min-w-0 flex-1 space-y-1">
              <a href={n.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-start gap-1 hover:underline">
                <span>{n.headline}</span><ExternalLink className="mt-1 h-3 w-3 shrink-0" aria-hidden />
              </a>
              <div className="flex flex-wrap gap-1.5">
                <Badge variant="accent">{n.symbol}</Badge>
                <Badge>{title(n.event_type)}{n.strength === "weak" ? " (weak)" : ""}</Badge>
                {n.source && <Badge variant="outline">{n.source.replace(/_news$/, "")}</Badge>}
                {n.sentiment && <Badge variant="outline">{n.sentiment.model}: +{(n.sentiment.pos * 100).toFixed(0)}% / −{(n.sentiment.neg * 100).toFixed(0)}%</Badge>}
              </div>
            </div>
          </article>
        ))}
      </Card>
    </div>
  );
}
