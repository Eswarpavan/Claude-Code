"use client";

import * as React from "react";
import { SWRConfig, type Cache } from "swr";
import { fetcher } from "@/lib/api";
import { LoginGate } from "@/components/login-gate";

const CACHE_KEY = "catalystedge.swr-cache";

/** Stale-while-revalidate across visits: the last responses are shown instantly on open,
 * then SWR refetches in the background. Stored in localStorage (per browser, non-critical). */
function persistentCache(): Cache {
  let initial: [string, unknown][] = [];
  try {
    initial = JSON.parse(window.localStorage.getItem(CACHE_KEY) ?? "[]");
  } catch {}
  const map = new Map(initial) as Cache;
  window.addEventListener("beforeunload", () => {
    try {
      window.localStorage.setItem(CACHE_KEY, JSON.stringify(Array.from((map as Map<string, unknown>).entries())));
    } catch {}
  });
  return map;
}

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig
      value={{
        fetcher,
        provider: typeof window === "undefined" ? undefined : persistentCache,
        revalidateOnFocus: true,
        dedupingInterval: 5000,
        keepPreviousData: true,
      }}
    >
      <LoginGate />
      {children}
    </SWRConfig>
  );
}
