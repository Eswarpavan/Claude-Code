import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("api client", () => {
  beforeEach(() => {
    vi.resetModules();
    window.sessionStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url === "/runtime-config") return new Response(JSON.stringify({ apiUrl: "http://api.test/" }));
      return handler(url, init);
    }));
    return calls;
  }

  it("sends the token as a header, never in the URL", async () => {
    const calls = stubFetch(() => new Response(JSON.stringify({ ok: 1 })));
    const { api, tokenStore } = await import("@/lib/api");
    tokenStore.set("tok123");
    await api("/api/signals");
    const call = calls.find((c) => c.url.startsWith("http://api.test"))!;
    expect(call.url).toBe("http://api.test/api/signals");
    expect((call.init!.headers as Record<string, string>).Authorization).toBe("Bearer tok123");
  });

  it("asks for login on 401", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: "login required" }), { status: 401 }));
    const { api } = await import("@/lib/api");
    const seen = vi.fn();
    window.addEventListener("catalystedge:login", seen);
    await expect(api("/api/signals")).rejects.toThrow("login required");
    expect(seen).toHaveBeenCalled();
  });

  it("parses server-sent events split across chunks", async () => {
    const enc = new TextEncoder();
    const body = new ReadableStream({
      start(c) {
        c.enqueue(enc.encode('data: {"a":1}\n\ndata: {"a"'));
        c.enqueue(enc.encode(':2}\n\n'));
        c.close();
      },
    });
    stubFetch(() => new Response(body));
    const { streamEvents } = await import("@/lib/api");
    const events: unknown[] = [];
    await streamEvents("/api/refresh/x/events", (d) => events.push(d));
    expect(events).toEqual([{ a: 1 }, { a: 2 }]);
  });
});
