"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

type Group = {
  n: number; qualify_at_fill_pct: number | null; still_0_5_pct: number | null; moved_5_15_pct: number | null;
  extended_pct: number | null; reversed_pct: number | null; skipped_by_gap_rule_pct: number | null;
  skipped_by_catalyst_rule_pct: number | null; enough_data?: boolean;
};
type FillCheck = {
  live: Group;
  backtest: { fresh_rules_signals: Group; all_fresh_events: Group; plain: string } | null;
  limitation: string;
};

const p = (x: number | null | undefined) => (x == null ? "–" : `${x.toFixed(0)}%`);

function Row({ name, g, note }: { name: string; g: Group; note?: string }) {
  return (
    <TR>
      <TD>{name}{note && <span className="block text-xs text-subtle">{note}</span>}</TD>
      <TD className="text-right">{g.n}</TD>
      <TD className="text-right font-medium">{p(g.qualify_at_fill_pct)}</TD>
      <TD className="text-right">{p(g.still_0_5_pct)}</TD>
      <TD className="text-right">{p(g.moved_5_15_pct)}</TD>
      <TD className="text-right">{p(g.extended_pct)}</TD>
      <TD className="text-right">{p(g.reversed_pct)}</TD>
      <TD className="text-right">{p(g.skipped_by_gap_rule_pct)}</TD>
      <TD className="text-right">{p(g.skipped_by_catalyst_rule_pct)}</TD>
    </TR>
  );
}

/** Of fresh 0-5% signals, how many are still buyable at the next-day open (the only time the app fills). */
export function FillCheckCard() {
  const { data } = useSWR<FillCheck>("/api/fill-check");
  if (!data) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Do fresh signals still qualify at the next-day open?</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted">
          Signals that were 0-5% above the pre-news price, checked again at the next day&apos;s open with the same two rules the
          paper account uses: skip if the open is more than 5% above the previous close, or more than 15% above / below the
          pre-news price.
        </p>
        <p className="text-xs text-warning-fg">{data.limitation}</p>
        <Table>
          <THead>
            <TR><TH>Group</TH><TH className="text-right">Fresh signals</TH><TH className="text-right">Qualify at fill</TH>
              <TH className="text-right">Still 0-5%</TH><TH className="text-right">Moved 5-15%</TH><TH className="text-right">Extended</TH>
              <TH className="text-right">Reversed</TH><TH className="text-right">Gap rule</TH><TH className="text-right">Catalyst rule</TH></TR>
          </THead>
          <TBody>
            {data.backtest && <Row name="Backtest: rules signals" g={data.backtest.fresh_rules_signals} />}
            {data.backtest && <Row name="Backtest: all positive events" g={data.backtest.all_fresh_events} />}
            <Row name="Live paper account" g={data.live} note={data.live.enough_data ? undefined : "not enough data yet (needs 30)"} />
          </TBody>
        </Table>
        {data.backtest?.plain && <p className="text-sm font-medium">{data.backtest.plain}</p>}
      </CardContent>
    </Card>
  );
}
