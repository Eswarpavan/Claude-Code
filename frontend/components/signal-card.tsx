"use client";

import * as React from "react";
import { useSWRConfig } from "swr";
import { ShoppingCart } from "lucide-react";
import { api } from "@/lib/api";
import { ago, pct, title, usd } from "@/lib/format";
import type { SignalT } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { TickerHover } from "@/components/ticker-hover";

export function SignalCard({ signal, rank }: { signal: SignalT; rank: number }) {
  const { mutate } = useSWRConfig();
  const [msg, setMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function buy() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ execute_on: string; notional_usd: number }>("/api/portfolio/buy", {
        method: "POST",
        body: JSON.stringify({ signal_id: signal.id }),
      });
      setMsg(`Paper order placed: ${usd(r.notional_usd)} at the ${r.execute_on} open.`);
      await mutate("/api/portfolio");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Order failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className={cn(signal.highlight && "ring-2 ring-accent")}>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-xs text-subtle">#{rank}</p>
            <TickerHover signal={signal}>
              <span className="text-lg font-semibold">{signal.symbol}</span>
            </TickerHover>{" "}
            <TickerHover signal={signal}>
              <span className="text-sm text-muted">{signal.company}</span>
            </TickerHover>
          </div>
          <div className="text-right">
            <p className="text-2xl font-semibold tabular">{signal.confidence.toFixed(0)}%</p>
            <Badge variant={signal.calibrated ? "good" : "warning"}>{signal.confidence_label}</Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="accent">{title(signal.catalyst)}</Badge>
          {signal.highlight && <Badge variant="good">≥ 80%</Badge>}
          <Badge variant="outline">News {ago(signal.hours_since_news)}</Badge>
        </div>
        <dl className="grid grid-cols-3 gap-2 text-sm tabular">
          <div>
            <dt className="text-xs text-subtle">Expected upside</dt>
            <dd className="font-medium">{pct(signal.expected_return_pct)}</dd>
          </div>
          <div>
            <dt className="text-xs text-subtle">Suggested size</dt>
            <dd className="font-medium">{usd(signal.suggested_size_usd)}</dd>
          </div>
          <div>
            <dt className="text-xs text-subtle">Stop-loss</dt>
            <dd className="font-medium">{usd(signal.stop_price)}</dd>
          </div>
        </dl>
        <p className="line-clamp-2 text-sm text-muted">{signal.headlines[0]?.headline ?? signal.reason}</p>
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-subtle">Rule {signal.rule_id}</span>
          <Button size="sm" onClick={buy} disabled={busy} aria-label={`Paper-buy ${signal.symbol} at the next open`}>
            <ShoppingCart className="h-3.5 w-3.5" aria-hidden /> Paper buy
          </Button>
        </div>
        {msg && <p className="text-xs text-muted" role="status">{msg}</p>}
      </CardContent>
    </Card>
  );
}
