# CatalystEdge dashboard

Next.js 16 (App Router) + TypeScript + Tailwind v4 + shadcn/ui components (copied into
`components/ui`, built on Radix), dark mode by default, SWR stale-while-revalidate.

```bash
pnpm install
pnpm dev            # http://localhost:3000 (expects the API on http://localhost:8000)
pnpm test           # vitest
pnpm lint && pnpm typecheck && pnpm build
```

The API address is read at runtime from `API_PUBLIC_URL` (or `NEXT_PUBLIC_API_URL` at build time)
through `/runtime-config`, so one Docker image works everywhere.

Not financial advice.
