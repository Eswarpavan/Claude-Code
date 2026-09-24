"use client";

import * as React from "react";
import { useSWRConfig } from "swr";
import { api, tokenStore } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Shows a password prompt when the API answers 401 (only when APP_PASSWORD is configured). */
export function LoginGate() {
  const [open, setOpen] = React.useState(false);
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const { mutate } = useSWRConfig();

  React.useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener("catalystedge:login", show);
    return () => window.removeEventListener("catalystedge:login", show);
  }, []);

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ token: string | null }>("/api/login", { method: "POST", body: JSON.stringify({ password }) });
      tokenStore.set(r.token);
      setOpen(false);
      setPassword("");
      await mutate(() => true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-label="Log in">
      <form onSubmit={submit} className="w-full max-w-sm space-y-3 rounded-lg border border-border bg-card p-5">
        <h2 className="text-lg font-semibold">Log in to CatalystEdge</h2>
        <p className="text-sm text-muted">Enter the APP_PASSWORD you set for this installation.</p>
        <Input type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" aria-label="Password" />
        {error && <p className="text-sm text-critical">{error}</p>}
        <Button type="submit" className="w-full">Log in</Button>
      </form>
    </div>
  );
}
