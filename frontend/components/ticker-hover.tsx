"use client";

import * as React from "react";
import { ExternalLink } from "lucide-react";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Badge } from "@/components/ui/badge";
import { ago, pct, title, usd } from "@/lib/format";
import type { SignalT } from "@/lib/types";

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
          <div>
            <p className="mb-1 text-xs font-medium text-subtle">Why</p>
            <p>{signal.reason}</p>
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
