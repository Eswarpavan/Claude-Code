"use client";

import * as React from "react";
import { ExternalLink } from "lucide-react";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Badge } from "@/components/ui/badge";
import { ago, pct, title, usd } from "@/lib/format";
import type { SignalT } from "@/lib/types";
import { TimesFMHoverLines } from "@/components/timesfm";

function SentimentBar({ s }: { s: NonNullable<SignalT["sentiment"]> }) {
  const parts = [
    ["Positive", s.pos, "bg-good"],
    ["Neutral", s.neu, "bg-foreground/30"],
    ["Negative", s.neg, "bg-critical"],
  ] as const;
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-full" aria-hidden>
        {parts.map(([k, v, c]) => (
          <div key={k} className={c} style={{ width: `${v * 100}%` }} />
        ))}
      </div>
      <p className="mt-1 text-xs text-subtle tabular">
        {parts.map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`).join(" · ")}
      </p>
    </div>
  );
}

/** Hover (or keyboard-focus) any ticker or company name to see the full signal. */
export function TickerHover({ signal, children }: { signal: SignalT; children: React.ReactNode }) {
  return (
    <HoverCard openDelay={120} closeDelay={80}>
      <HoverCardTrigger asChild>
        <button type="button" className="cursor-help text-left underline decoration-dotted underline-offset-4">
          {children}
        </button>
      </HoverCardTrigger>
      <HoverCardContent>
        <div className="space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="font-semibold">
                {signal.symbol} · {signal.company}
              </p>
              <p className="text-xs text-subtle">
                {title(signal.catalyst)} · rule {signal.rule_id}
              </p>
            </div>
            <Badge variant={signal.calibrated ? "good" : "warning"}>{signal.confidence_label}</Badge>
          </div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm tabular">
            <dt className="text-subtle">Confidence</dt>
            <dd>{signal.confidence.toFixed(0)}%</dd>
            <dt className="text-subtle">Expected return</dt>
            <dd>
              {pct(signal.expected_return_pct)} <span className="text-xs text-subtle">({signal.expected_return_basis})</span>
            </dd>
            <dt className="text-subtle">Holding period</dt>
            <dd>
              {signal.holding_days[0]}–{signal.holding_days[1]} trading days
            </dd>
            <dt className="text-subtle">Stop-loss</dt>
            <dd>{usd(signal.stop_price)}</dd>
            <dt className="text-subtle">Target</dt>
            <dd>{usd(signal.target_price)}</dd>
            <dt className="text-subtle">Suggested size</dt>
            <dd>{usd(signal.suggested_size_usd)}</dd>
          </dl>
          {signal.sentiment && (
            <div>
              <p className="mb-1 text-xs font-medium text-subtle">Sentiment ({signal.sentiment_model ?? "model"})</p>
              <SentimentBar s={signal.sentiment} />
              {signal.model_disagrees && (
                <p className="mt-1 text-xs text-warning-fg">The sentiment model disagrees with the rule; confidence was lowered.</p>
              )}
            </div>
          )}
          <div>
            <p className="mb-1 text-xs font-medium text-subtle">Key headlines</p>
            <ul className="space-y-1">
              {signal.headlines.map((h) => (
                <li key={h.url + h.at}>
                  <a href={h.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-start gap-1 hover:underline">
                    <span>{h.headline}</span>
                    <ExternalLink className="mt-1 h-3 w-3 shrink-0" aria-hidden />
                  </a>
                  <span className="block text-xs text-subtle">
                    {h.source?.replace(/_news$/, "") ?? "news"} · {new Date(h.at).toLocaleString()}
                  </span>
                </li>
              ))}
            </ul>
          </div>
          {signal.market_check && signal.market_check.move_since_catalyst_pct != null && (
            <div className="text-xs">
              <p>
                <span className="font-medium text-subtle">Move since the news: </span>
                <span className={signal.market_check.extended_status === "extended" || signal.market_check.extended_status === "reversed" ? "text-warning-fg" : ""}>
                  {signal.market_check.move_since_catalyst_pct > 0 ? "+" : ""}{signal.market_check.move_since_catalyst_pct.toFixed(1)}% ({signal.market_check.extended_label})
                </span>
                <span className="text-subtle"> · {signal.market_check.price_source}</span>
              </p>
              {signal.market_check.relative_volume != null && (
                <p className="text-subtle" title={signal.market_check.relative_volume_note}>
                  Relative volume {signal.market_check.relative_volume.toFixed(1)}x (end of day, vs 20-day average)
                </p>
              )}
              <p className="text-subtle">Information only: the early-move idea has not passed the backtest.</p>
            </div>
          )}
          {signal.short_interest && <p className="text-xs text-subtle">{signal.short_interest.text} Context only.</p>}
          {signal.verification && (
            <p className="text-xs">
              <span className="font-medium text-subtle">Source check: </span>
              {signal.verification === "primary" ? "from an original source (SEC filing, newswire, company or agency)"
                : signal.verification === "verified" ? "news report confirmed by an original source"
                : <span className="text-warning-fg">unverified: news-aggregator reports only so far</span>}
            </p>
          )}
          {signal.filing_flags && signal.filing_flags.length > 0 && (
            <div>
              <p className="mb-1 text-xs font-medium text-subtle">Recent SEC filings (context, not in the score)</p>
              <ul className="space-y-0.5 text-xs">
                {signal.filing_flags.slice(0, 4).map((f) => (
                  <li key={f.url} className={f.tone === "negative" ? "text-warning-fg" : "text-muted"}>
                    <a href={f.url} target="_blank" rel="noopener noreferrer" className="hover:underline">{f.form}</a>{" "}
                    {f.filed_at.slice(0, 10)}: {f.text}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <TimesFMHoverLines tag={signal.timesfm} />
          <div>
            <p className="mb-1 text-xs font-medium text-subtle">Why</p>
            <p>{signal.reason}</p>
            {signal.llm_why && (
              <div className="mt-2 rounded-md border border-border p-2">
                <p className="text-xs font-medium text-subtle">In plain English (local AI)</p>
                <p className="text-sm">{signal.llm_why.text}</p>
                <p className="mt-1 text-xs text-subtle">{signal.llm_why.label}</p>
              </div>
            )}
            {signal.rule_components && (
              <p className="mt-1 text-xs text-subtle tabular">
                {Object.entries(signal.rule_components)
                  .filter(([k, v]) => k !== "base" && v !== 0)
                  .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
                  .slice(0, 5)
                  .map(([k, v]) => `${k.replace(/_/g, " ")} ${v > 0 ? "+" : ""}${v.toFixed(0)}`)
                  .join(" · ")}
              </p>
            )}
            {signal.reaction_since_news_pct != null && (
              <p className="text-xs text-subtle">Price since the news: {pct(signal.reaction_since_news_pct)}</p>
            )}
            {signal.shap?.top?.length ? (
              <ul className="mt-1 list-inside list-disc text-xs text-muted">
                {signal.shap.top.slice(0, 3).map((x) => <li key={x.feature}>{x.plain}</li>)}
              </ul>
            ) : null}
          </div>
          {signal.risk_notes.length > 0 && (
            <div>
              <p className="mb-1 text-xs font-medium text-subtle">Risk notes</p>
              <ul className="list-inside list-disc text-sm">
                {signal.risk_notes.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-xs text-subtle">News {ago(signal.hours_since_news)} · as of close {signal.as_of_date}</p>
        </div>
      </HoverCardContent>
    </HoverCard>
  );
}
