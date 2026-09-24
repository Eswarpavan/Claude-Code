"use client";

// Browser-side API client. The login token lives in sessionStorage (cleared when the tab closes)
// and is sent as a header only, never in a URL.

let apiBase: Promise<string> | null = null;

export function apiUrl(): Promise<string> {
  apiBase ??= fetch("/runtime-config")
    .then((r) => r.json())
    .then((c: { apiUrl: string }) => c.apiUrl.replace(/\/$/, ""))
    .catch(() => "http://localhost:8000");
  return apiBase;
}

const TOKEN_KEY = "catalystedge.token";

export const tokenStore = {
  get: () => (typeof window === "undefined" ? null : window.sessionStorage.getItem(TOKEN_KEY)),
  set: (t: string | null) => (t ? window.sessionStorage.setItem(TOKEN_KEY, t) : window.sessionStorage.removeItem(TOKEN_KEY)),
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = await apiUrl();
  const token = tokenStore.get();
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    if (res.status === 401 && typeof window !== "undefined") window.dispatchEvent(new Event("catalystedge:login"));
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export const fetcher = <T,>(path: string) => api<T>(path);

/** Server-sent events over fetch (so the auth header works); calls onEvent for each `data:` line. */
export async function streamEvents(path: string, onEvent: (data: unknown) => void, signal?: AbortSignal) {
  const base = await apiUrl();
  const token = tokenStore.get();
  const res = await fetch(`${base}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal });
  if (!res.ok || !res.body) throw new ApiError(res.status, "stream failed");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
