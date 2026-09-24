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

const noopSubscribe = () => () => {};

export function Providers({ children }: { children: React.ReactNode }) {
  // The saved cache is only attached after hydration; attaching it during the first render would
  // make the client HTML differ from the server's (React error #418). The key remounts SWR once.
  const hydrated = React.useSyncExternalStore(noopSubscribe, () => true, () => false);
  return (
    <SWRConfig
      key={hydrated ? "persisted" : "ssr"}
      value={{
        fetcher,
        provider: hydrated ? persistentCache : undefined,
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
