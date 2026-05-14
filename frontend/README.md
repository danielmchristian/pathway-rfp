# Pathway RFP — Frontend

Next.js 14 (App Router) + TypeScript + Tailwind. Single-page pipeline
visualization that renders against the live backend at
`NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`).

## Quick start

Backend must be running first (`make dev` from the repo root, or
`docker compose up`).

```bash
cd frontend
cp .env.local.example .env.local       # picks up NEXT_PUBLIC_API_BASE_URL
npm install
npm run dev                            # http://localhost:3000
```

Production-ish build:

```bash
npm run build
npm run start
```

## Layout

```
app/
  layout.tsx     # root layout, dark by default
  page.tsx       # single-page dashboard — server-rendered, all 6 stages
  globals.css    # Tailwind base + small component layer (.card, .pill, .btn)
components/
  stages/        # one component per pipeline stage
  Card.tsx       # StageSection / Panel / EmptyState
  CostDashboard.tsx
  ConfidenceBadge.tsx
  TrendIndicator.tsx
  EmailBodyModal.tsx
  PipelineTriggers.tsx   # client — POSTs poll_inbox / finalize, then router.refresh
  SseEventStream.tsx     # client — listens to /api/restaurants/{id}/events
  Header.tsx
lib/
  api.ts         # fetch wrappers for the backend
  types.ts       # TS shapes mirroring backend Pydantic schemas
```

## Design notes

- **Dark mode only.** Reads cleanly on a Loom recording.
- One accent (emerald) — used for the brand mark, the recommendation pick,
  and the high-confidence badge tier. Everything else is `zinc`.
- Numbers in `font-mono` (`.num`) so prices/IDs align without effort.
- **Traffic-light confidence** on parser outputs: green ≥ 0.8, amber 0.5–0.8,
  rose < 0.5. Pills carry the numeric value too.
- **Asymmetric trend color**: rising price = rose (bad for the buyer);
  falling = emerald.
- **Empty states for Stages 5 & 6** when no quotes have been collected.
  The footer triggers (Poll inbox / Finalize) populate them in place via
  `router.refresh()` — no jank, no page flash.

## Triggers

Footer buttons call:

- `POST /api/rfp/{id}/poll_inbox` — runs one IMAP poll cycle. Updates
  feedback inline, then `router.refresh()` re-renders Stages 4–6 with the
  newly parsed quotes / follow-ups / recommendation.
- `POST /api/rfp/{id}/finalize` — force-computes the recommendation
  regardless of completeness. Same in-place refresh.

## SSE live events

The footer also subscribes to `/api/restaurants/{id}/events` (Server-Sent
Events). Stage transitions (`inbox_poll:start`, `quote_parse:complete`,
`followup:complete`, `recommendation:complete`, etc.) appear as a live
tail next to the connection indicator.
