"use client";

import useSWR from "swr";
import { pct, title } from "@/lib/format";
import type { BacktestReport, CatalystReport, Stat } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const hit = (s?: Stat | null) => (s && s.hit_rate != null ? `${(s.hit_rate * 100).toFixed(0)}%` : "–");
const avg = (s?: Stat | null) => (s && s.mean_pct != null ? pct(s.mean_pct, 2) : "–");
const shp = (s?: Stat | null) => (s && s.sharpe != null ? s.sharpe.toFixed(2) : "–");

const STRATEGY_LABEL: Record<string, string> = {
  rules: "CatalystEdge rules (confidence ≥ 65)",
  model: "Rules + LightGBM ranking model",
  all_events: "Naive: buy every positive event",
  spy_same_days: "Buy the S&P 500 (same days)",
};

function StatRow({ name, s, strong }: { name: string; s?: Stat | null; strong?: boolean }) {
  return (
    <TR>
      <TD className={strong ? "font-medium" : undefined}>{name}</TD>
      <TD className="text-right tabular">{s?.n ?? 0}</TD>
      <TD className="text-right tabular">{hit(s)}</TD>
      <TD className="text-right tabular">{avg(s)}</TD>
      <TD className="text-right tabular">{shp(s)}</TD>
    </TR>
  );
}

function StatHead({ first }: { first: string }) {
  return (
    <THead>
      <TR>
        <TH>{first}</TH>
        <TH className="text-right">Trades</TH>
        <TH className="text-right">Hit rate</TH>
        <TH className="text-right">Avg return (after costs)</TH>
        <TH className="text-right">Sharpe</TH>
      </TR>
    </THead>
  );
}

/** Walk-forward result: strategies vs baselines, verdict, confidence buckets, TimesFM with vs without. */
export function BacktestSummary() {
  const { data } = useSWR<{ run: { id: number; finished_at: string } | null; report?: BacktestReport; how_to_run?: string }>(
    "/api/backtest/latest",
  );
  if (!data) return null;
  if (!data.run || !data.report)
    return (
      <Card>
        <CardHeader><CardTitle>Walk-forward backtest</CardTitle></CardHeader>
        <CardContent className="text-sm text-muted">No backtest yet. Run: <code>{data.how_to_run}</code></CardContent>
      </Card>
    );
  const r = data.report;
  const v = r.verdict;
  const t = r.timesfm;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Walk-forward backtest (out of sample{r.period ? `, ${r.period[0]} to ${r.period[1]}` : ""})</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="flex flex-wrap items-center gap-2">
          <Badge variant={v.model_enabled ? "good" : "outline"}>{v.model_enabled ? "Model ON" : "Model OFF"}</Badge>
          <Badge variant={v.rules_beat_baselines ? "good" : "warning"}>
            {v.rules_beat_baselines ? "Rules beat baselines" : "Rules did not beat baselines"}
          </Badge>
        </p>
        <p>{v.plain}</p>
        <Table>
          <StatHead first="Strategy" />
          <TBody>
            {(["rules", "model", "all_events", "spy_same_days"] as const).map((k) => (
              <StatRow key={k} name={STRATEGY_LABEL[k]} s={r.strategies[k]} strong={k === "rules"} />
            ))}
          </TBody>
        </Table>
        <div>
          <p className="mb-1 text-xs font-medium text-subtle">Hit rate by confidence bucket (rules)</p>
          <Table>
            <StatHead first="Confidence" />
            <TBody>
              {r.confidence_buckets.rules.map((b) => <StatRow key={b.bucket} name={b.bucket} s={b} />)}
            </TBody>
          </Table>
        </div>
        {r.priced_in_skip && (
          <p className="text-xs text-muted">
            Priced-in skip check: skipped events averaged {avg(r.priced_in_skip.skipped)} ({r.priced_in_skip.skipped.n} trades) vs
            {" "}{avg(r.priced_in_skip.kept)} for kept ones.
          </p>
        )}
        {t && (
          <div className="space-y-2 border-t border-border pt-3">
            <p className="font-medium">TimesFM: with vs without</p>
            <p className={t.helps ? "text-good" : "text-warning-fg"}>{t.plain}</p>
            <Table>
              <StatHead first="Variant" />
              <TBody>
                <StatRow name="Rules without TimesFM" s={t.rules_without_timesfm} />
                <StatRow name="Rules with TimesFM filter" s={t.rules_with_timesfm_filter} strong />
                <StatRow name="Naive: every positive event" s={t.naive_all_events} />
                <StatRow name="S&P 500 (same days)" s={t.spy_same_days} />
              </TBody>
            </Table>
            {t.leakage_warning && <p className="text-xs text-warning-fg">{t.leakage_warning}</p>}
          </div>
        )}
        <ul className="list-inside list-disc text-xs text-subtle">
          {r.caveats.map((c) => <li key={c}>{c}</li>)}
        </ul>
      </CardContent>
    </Card>
  );
}

/** Which catalysts actually work: live outcomes and the backtest, side by side. */
export function CatalystTable() {
  const { data } = useSWR<CatalystReport>("/api/catalysts");
  if (!data) return null;
  const cats = Array.from(new Set([...Object.keys(data.live), ...Object.keys(data.backtest.rules), ...Object.keys(data.backtest.all_events)]));
  return (
    <Card>
      <CardHeader><CardTitle>Hit rate and average return by catalyst</CardTitle></CardHeader>
      <CardContent className="space-y-2">
        {cats.length === 0 ? <p className="text-sm text-muted">No outcomes yet. Live results appear 1, 3 and 10 trading days after each signal.</p> : (
          <Table>
            <THead>
              <TR>
                <TH>Catalyst</TH>
                <TH className="text-right">Live 10-day (n · hit · avg)</TH>
                <TH className="text-right">Backtest rules (n · hit · avg)</TH>
                <TH className="text-right">Backtest all events (n · hit · avg)</TH>
              </TR>
            </THead>
            <TBody>
              {cats.map((c) => {
                const live = data.live[c]?.["10d"];
                const br = data.backtest.rules[c];
                const ba = data.backtest.all_events[c];
                return (
                  <TR key={c}>
                    <TD>{title(c)}</TD>
                    <TD className="text-right tabular">{live ? `${live.n} · ${hit(live)} · ${avg(live)}` : "–"}</TD>
                    <TD className="text-right tabular">{br ? `${br.n} · ${hit(br)} · ${avg(br)}` : "–"}</TD>
                    <TD className="text-right tabular">{ba ? `${ba.n} · ${hit(ba)} · ${avg(ba)}` : "–"}</TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
        <p className="text-xs text-subtle">{data.evidence.plain}</p>
      </CardContent>
    </Card>
  );
}
