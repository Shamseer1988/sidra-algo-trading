# Implementation Plan

## Phase 1 — Foundation (current)

- Establish the monorepo, FastAPI service, scanner process, Next.js shell, PostgreSQL, Redis, Docker Compose, environment template, and Windows helper scripts.
- Add a production-oriented API configuration, database session factory, initial user/session/audit models, health endpoint, auth primitives, and admin bootstrap command.
- Keep `LIVE_TRADING_ENABLED=false` immutable by default.

## Phase 2 — Application shell and access control (completed)

- Completed: login UX, RBAC middleware, account lockout, paper-risk settings, scanner control surface, system-health interface, and dark terminal layout.
- Completed hardening: refresh-token rotation, CSRF protection, Redis-backed login rate limits, session revocation, and audit-log views.

## Phase 3 — Market-data connectors and ingestion (completed)

- Implement the Upstox V3 paper-market-data adapter using its official SDK, with an
  exclusive persisted connector selector. Firstock remains a disabled-by-default
  alternative feed. Order, position, and live-trading streams remain inactive.
- Completed hardening: state-validated server-side OAuth code exchange, encrypted
  token persistence, and automated NSE instrument-master validation refresh.

## Phase 4 — Market calculations (completed)

- Completed tick-to-candle aggregation, complete-candle persistence, session VWAP,
  EMAs, volume metrics, opening range, timestamp-aligned relative strength, and
  NIFTY regime. Calculations reject pre-/post-market ticks and use completed candles only.

## Phase 5 — Strategy and scanner (completed)

- Completed configurable long/short state machines, EMA-spread chop filtering, score
  breakdowns, risk/target calculations, daily risk and signal limits, duplicate
  prevention, scanner orchestration, and deterministic completed-candle evaluation.

## Phase 6 — Realtime UI (completed)

- Completed dashboard, scanner table, authenticated completed-candle charts, stock
  detail, native WebSocket event fan-out, system status, sorting/filtering, and
  responsive terminal components.

## Phase 7 — Telegram

- Add outbound-only notification service, encrypted-at-rest configuration, Redis cooldown/duplication controls, delivery history, and test notifications.

## Phase 8 — Journal and analytics (completed)

- Completed: completed-candle-only paper outcome tracking with MFE/MAE and realized R,
  authenticated performance summaries, and CSV export.

## Phase 9 — Replay and quality (in progress)

- In progress: deterministic completed-candle replay driver and unit coverage.
  Synthetic scenarios, browser coverage, load checks, and failure/recovery tests remain.

## Phase 10 — Deployment hardening

- Add migration/runbooks, backups and restores, log rotation, security headers/CSP/rate limits, Windows deployment guide, and release checklist.

Each phase ends with tests, documentation updates, and a review of the live-trading safety boundary.
