"use client";

import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type LLMStatus = { enabled: boolean; model: string; status: "off" | "ready" | "unreachable" | "model_missing" | string; what_it_does: string; how_to_turn_on: string };

const TEXT: Record<string, [string, "good" | "warning" | "outline"]> = {
  off: ["Off", "outline"],
  ready: ["On and working", "good"],
  unreachable: ["On, but Ollama is not running: signals work normally without explanations", "warning"],
  model_missing: ["On, but the model is not downloaded yet", "warning"],
};

/** Read-only: the local AI explanations are switched in .env (LLM_ENABLED), not from the browser. */
export function LLMSettings() {
  const { data } = useSWR<LLMStatus>("/api/llm");
  if (!data) return null;
  const [label, variant] = TEXT[data.status] ?? [`Problem: ${data.status}`, "warning" as const];
  return (
    <Card>
      <CardHeader><CardTitle>Local AI explanations (Ollama, optional)</CardTitle></CardHeader>
      <CardContent className="space-y-2 text-sm">
        <p><Badge variant={variant}>{label}</Badge> <span className="text-subtle">model {data.model}</span></p>
        <p className="text-muted">{data.what_it_does}</p>
        {data.status !== "ready" && <p className="text-xs text-subtle">{data.how_to_turn_on}</p>}
      </CardContent>
    </Card>
  );
}
