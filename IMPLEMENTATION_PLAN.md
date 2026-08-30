# Implementation Plan

## Phase 1 — Foundation (current)

- Establish the monorepo, FastAPI service, scanner process, Next.js shell, PostgreSQL, Redis, Docker Compose, environment template, and Windows helper scripts.
- Add a production-oriented API configuration, database session factory, initial user/session/audit models, health endpoint, auth primitives, and admin bootstrap command.
- Keep `LIVE_TRADING_ENABLED=false` immutable by default.

## Phase 2 — Application shell and access control

- Finish login UX, refresh-token rotation, CSRF, RBAC middleware, settings views, system-health interface, dark terminal layout, and audit views.

## Phase 3 — Firstock and market ingestion

- Verify the current official Firstock API documentation immediately before implementation.
- Implement an isolated REST/auth adapter and WebSocket V2 market-feed adapter, instrument import, reconnect/heartbeat/staleness handling, and connection telemetry. Order and position streams remain inactive.

## Phase 4 — Market calculations

- Build tick-to-candle aggregation, complete-candle persistence, session VWAP, EMAs, volume metrics, opening range, relative strength, and NIFTY regime; unit-test each independently.

## Phase 5 — Strategy and scanner

- Implement configurable long/short state machines, chop filters, score breakdowns, risk/target calculations, duplicate prevention, scanner orchestration, and historical/replay-safe evaluation.

## Phase 6 — Realtime UI

- Add dashboard, scanner table, charts, stock detail, native WebSocket event fan-out, system status, sorting/filtering, and responsive terminal components.

## Phase 7 — Telegram

- Add outbound-only notification service, encrypted-at-rest configuration, Redis cooldown/duplication controls, delivery history, and test notifications.

## Phase 8 — Journal and analytics

- Track paper signals without look-ahead bias, calculate outcome/MFE/MAE/R, and provide analytics and exports.

## Phase 9 — Replay and quality

- Add replay driver, synthetic scenarios, component tests, Playwright coverage, load checks, and failure/recovery testing.

## Phase 10 — Deployment hardening

- Add migration/runbooks, backups and restores, log rotation, security headers/CSP/rate limits, Windows deployment guide, and release checklist.

Each phase ends with tests, documentation updates, and a review of the live-trading safety boundary.
