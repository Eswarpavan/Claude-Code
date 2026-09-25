"use client";

import useSWR from "swr";
import { ExternalLink } from "lucide-react";
import type { TopSignal, TopSignalsResponse } from "@/lib/types";
import { pct, title } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { CalibrationBanner } from "@/components/calibration-banner";

function History({ h }: { h: TopSignal["history"] }) {
  if (!h) return <p className="text-sm text-muted">No backtest evidence for this catalyst.</p>;
  if (!h.n) return <p className="text-sm text-muted">{h.text}</p>;
  return (
    <div className="space-y-1">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm tabular sm:grid-cols-4">
        <div><dt className="text-xs text-subtle">Avg per trade</dt><dd className="font-medium">{pct(h.avg_return_pct, 2)}</dd></div>
        <div><dt className="text-xs text-subtle">Win rate</dt><dd className="font-medium">{h.win_rate != null ? `${Math.round(h.win_rate * 100)}%` : "–"}</dd></div>
        <div><dt className="text-xs text-subtle">Trades</dt><dd className="font-medium">{h.n}</dd></div>
        <div><dt className="text-xs text-subtle">S&amp;P 500, same days</dt><dd>{pct(h.spy_avg_return_pct, 2)}{h.spy_win_rate != null ? ` · ${Math.round(h.spy_win_rate * 100)}%` : ""}</dd></div>
      </dl>
      {!h.enough_data && <p className="text-xs text-warning-fg">Too few trades to rely on (needs 30).</p>}
    </div>
  );
}

function Row({ s, rank, unreliable }: { s: TopSignal; rank: number; unreliable: boolean }) {
  const e = s.expected;
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-xs text-subtle">#{rank}</p>
            <p className="font-semibold">{s.symbol} <span className="font-normal text-muted">{s.company}</span></p>
            <Badge variant="outline" className="mt-1">{title(s.catalyst)}</Badge>
          </div>
          <div className="text-right">
            <p className="text-2xl font-semibold tabular">{s.confidence.toFixed(0)}</p>
            <p className="text-xs text-subtle">confidence · {s.confidence_label}</p>
            {unreliable && <p className="text-xs text-warning-fg">does not rank trades (backtest)</p>}
          </div>
        </div>
        {s.headline && (
          <a href={s.headline.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-start gap-1 text-sm hover:underline">
            <span>{s.headline.headline}</span><ExternalLink className="mt-1 h-3 w-3 shrink-0" aria-hidden />
          </a>
        )}
        <p className="text-sm text-muted tabular">
          Rule estimate {pct(e.prior_return_pct, 1)} over {e.holding_days[0]}–{e.holding_days[1]} trading days · stop {pct(e.stop_pct, 1)} · target {pct(e.target_pct, 1)}
          <span className="block text-xs text-subtle">A starting assumption for this catalyst type, not a backtest result or a promise.</span>
        </p>
        <div className="border-t border-border pt-3">
          <p className="mb-1 text-xs font-medium text-subtle">What this catalyst type actually did{s.history?.source ? ` (${s.history.source})` : ""}</p>
          <History h={s.history} />
        </div>
      </CardContent>
    </Card>
  );
}

export default function TopSignalsPage() {
  const { data, error } = useSWR<TopSignalsResponse>("/api/signals/top");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Top signals</h1>
        <p className="text-sm text-muted">The 10 highest-confidence signals right now, each next to what its catalyst type has actually done in the backtest (after costs). No profit is promised.</p>
      </div>
      <CalibrationBanner />
      {data?.confidence_check && !data.confidence_check.reliable && (
        <div role="alert" className="rounded-md border border-warning-fg/40 bg-warning-bg p-3 text-sm text-warning-fg">
          <p className="font-medium">The confidence number is not a reliable ranking.</p>
          <p>{data.confidence_check.text}</p>
        </div>
      )}
      {error && <p className="text-sm text-critical">{error.message}</p>}
      {!data && !error && <p className="text-sm text-muted">Loading…</p>}
      {data && data.signals.length === 0 && <p className="text-sm text-muted">No signals at or above 65 right now. That is normal on quiet news days.</p>}
      {data && data.signals.length > 0 && (
        <>
          <p className="text-xs text-subtle">As of the {data.as_of_date} close.</p>
          <div className="grid gap-3 lg:grid-cols-2">{data.signals.map((s, i) => <Row key={s.id} s={s} rank={i + 1} unreliable={data.confidence_check?.reliable === false} />)}</div>
        </>
      )}
    </div>
  );
}
