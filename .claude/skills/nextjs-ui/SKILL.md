---
name: nextjs-ui
description: AgentOps frontend conventions — Next.js App Router server/client component boundaries, the useJobSocket WebSocket hook contract with reconnect and backfill, status-to-color mapping, Tailwind usage, and the hand-rolled SVG pipeline diagram. Use when adding or changing anything under frontend/src/, a page, a component, or WebSocket client code.
---

# Next.js Frontend Conventions

Pages and components are fixed by TECH_SPEC §7.1 and §7.2.

```
frontend/src/
  app/page.tsx                Dashboard: submit form + live job queue
  app/jobs/[jobId]/page.tsx   Run detail: diagram + logs + output
  components/JobSubmitForm.tsx
  components/JobQueueList.tsx
  components/PipelineDiagram.tsx
  components/LogPanel.tsx
  components/RunSummary.tsx
  hooks/useJobSocket.ts
  lib/api.ts                  typed REST client
  lib/events.ts               WS event types — mirror of TECH_SPEC §6
  lib/status.ts               single source of status→color mapping
```

## Server vs client boundary

TECH_SPEC §7.3. Initial data comes from the server; live updates from the client.

- **Server Components** (default, no directive): initial fetch of job history and
  run detail. These run on the server, so they use the internal backend URL and
  can never leak a secret to the browser.
- **Client Components** (`"use client"`): anything with `useState`, `useEffect`, a
  WebSocket, or an event handler — the diagram, the log panel, the form.

Keep `"use client"` as low in the tree as possible. Marking a page as client
throws away the server-rendered initial state and reintroduces a loading spinner
the architecture was designed to avoid.

Pass server-fetched data down as props to seed client state. Do not refetch on
mount what the server already rendered.

## Environment URLs — two of them, deliberately

- `BACKEND_INTERNAL_URL` — server-side only (in Docker: `http://backend:8000`)
- `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` — browser-visible

Only `NEXT_PUBLIC_*` values reach the browser. Never put a secret behind a
`NEXT_PUBLIC_` prefix; per TECH_SPEC §1.1 the frontend holds no keys at all, so
there should be nothing to leak.

Derive the WS scheme from the page protocol (`wss:` on https, `ws:` on http)
rather than hardcoding, or the deployed build breaks under TLS.

## useJobSocket — the reconnect and backfill contract

TECH_SPEC §6 "Reconnection Behavior". The hook must:

1. Connect to `${WS_URL}/ws/jobs/${jobId}`
2. Track `lastSeenTimestamp` from every received event
3. On unexpected close, reconnect with **exponential backoff and jitter**, capped
   (e.g. 1s → 30s). No unbounded-frequency retry loops.
4. On reconnect, backfill before resuming live render: `GET /jobs/{jobId}` for
   current state and `GET /jobs/{jobId}/logs?since=<lastSeenTimestamp>` for missed
   logs
5. **Deduplicate by event id / timestamp** — backfill and live stream overlap by
   design, and duplicate log lines in the panel is the visible symptom of skipping
   this step
6. Clean up in the effect's return: close the socket, clear timers, and guard
   against `setState` after unmount

Use a `ref` for the socket and for reconnect attempt count. Putting the socket in
state causes a reconnect on every render.

Do not reconnect when the job has reached a terminal status (`completed` /
`failed`) — the server closes the channel and retrying forever wastes cycles and
looks broken in the network tab.

## Status → color mapping lives in exactly one place

PRD §7.2 defines the colors. Put the map in `lib/status.ts` and import it
everywhere (diagram, queue badges, log rows). A second copy will drift.

| Status | Color | Treatment |
|---|---|---|
| idle | gray | flat |
| queued | yellow | flat |
| running | blue | animated (pulse) |
| completed | green | flat |
| failed | red | flat |
| retrying | orange | animated |

Never encode state by color alone — carry a text label or icon too, or the
diagram is unreadable to a colorblind viewer and in a grayscale screenshot.
Respect `prefers-reduced-motion` for the running/retrying animations.

## PipelineDiagram — hand-rolled SVG

TECH_SPEC §2 explicitly chooses hand-rolled SVG over a graph library because the
topology is fixed (Supervisor → Clarifier → Worker(s) → Verifier → Done). Do not
add `reactflow`, `d3`, or `dagre` for v1.

Node positions are computed from the ordered `agent_runs` array. Nodes carry
`role="img"` with an `aria-label` stating agent name and status.

## LogPanel

Appends on `log_line` events, newest at the bottom, with an auto-scroll that
**stops when the user scrolls up** and resumes at the bottom — an auto-scroll that
fights the user is worse than none.

Cap retained lines in memory (e.g. 2000) and note the truncation in the UI. The
per-agent filter is derived from the agents present in the run, not hardcoded.

Every log row needs a stable `key` — use the log id from the backend, never the
array index, or React will mis-diff on filter changes.

## Tailwind

Utility classes inline. No CSS-in-JS, no separate module stylesheets. Shared
visual patterns become components, not `@apply` soup. Dark theme is the baseline
for this dashboard.

## TypeScript

`strict: true`. No `any` — an unknown WS payload is `unknown` and gets narrowed.
`lib/events.ts` is a discriminated union on `event_type`, so a `switch` over it is
exhaustively checked; add a `never`-typed default case so a new event type
becomes a compile error rather than a silent no-op.

## Testing

Vitest + Testing Library. The three required cases from TECH_SPEC §10:
`PipelineDiagram` status→color mapping, `LogPanel` append behavior, and
`useJobSocket` reconnect logic against a mock WebSocket.
