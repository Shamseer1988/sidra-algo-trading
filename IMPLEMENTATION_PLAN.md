# Sidra Algo Trading V2 — Implementation Plan

This plan supersedes the earlier Intraday Sentinel release plan. The V2 master prompt is the product brief; the existing paper-only platform remains the implementation baseline.

## Delivery principles

- Preserve working paper-scanner behavior and data.
- Keep `LIVE_TRADING_ENABLED=false` and provide no broker order path until execution, risk, reconciliation, compliance, and operator gates are complete.
- Ship database changes only through Alembic migrations.
- End every phase with backend tests, frontend lint/build, critical browser journeys, documentation, and a safety-boundary review.

## Audit baseline — completed 1 September 2026

### Confirmed strengths

- FastAPI, PostgreSQL, Redis, Next.js, authenticated WebSockets, Docker, Alembic, RBAC, CSRF, refresh-token rotation, account lockout, audit history, scanner controls, paper signals, journal analytics, replay foundations, Upstox and Firstock market-data adapters, and Telegram configuration already exist.
- Backend baseline: 31 tests passed.
- Frontend production build passed.

### Confirmed gaps

- The previous roadmap no longer represented the V2 scope.
- Upstox candle assignment used local receipt time instead of last-trade/exchange time.
- Persisted strategy cards were not consumed by the scanner.
- The browser API layer did not perform single-flight access-token refresh.
- WebSockets hardcoded public port 8000.
- CI still targeted `develop`.
- Docker publicly exposed FastAPI and application containers ran as root.
- Unsupported Playwright/PIN/TOTP broker-login automation remained present.
- Frontend lint had 17 errors, the shell was concentrated in one component, and only a dark theme existed.
- Trading calendar, data-quality gates, professional OMS, risk reservation, reconciliation, backtesting platform, and production reverse-proxy topology remain incomplete.

## Phase 0 — Baseline and roadmap alignment (complete)

- Audited repository, Git state, architecture, tests, configuration, Docker, migrations, broker adapters, scanner, authentication, Telegram, and frontend shell.
- Replaced the obsolete roadmap with this V2 plan.
- Cleared baseline lint and formatting failures.

## Phase 1 — Safety foundation (in progress)

Completed:

- CI now targets `main` pushes and pull requests.
- Market ticks retain exchange and received timestamps; candle bucketing uses exchange time and latency is tracked separately.
- Persisted enabled strategy configurations now drive scanner evaluation with per-version state and signal keys.
- API requests use single-flight refresh and retry once; unrelated server failures no longer force login redirects.
- Scanner WebSockets use same-origin `ws/wss`.
- FastAPI is internal-only in Compose; API, worker, and web containers run non-root with dropped capabilities.
- Unsupported broker credential/TOTP browser automation and its runtime dependencies were removed.
- Authoritative encrypted Telegram configuration remains shared across status, notifications, and callbacks.

Remaining:

- Trading Calendar with holidays and special sessions.
- Market-data quality state and execution blocking.
- Structured worker supervision and recovery improvements.
- Production reverse proxy, TLS, headers, and startup configuration validation.

## Phase 2 — Premium terminal UI (started)

Completed first slice:

- Sidra Algo visual identity and command-center information hierarchy.
- Semantic Light, Dark, and System themes with persisted preference and no initial theme flash.
- Compact responsive sidebar with collapsed icon mode and mobile drawer.
- Global header with real scanner/feed state, PAPER badge, and live IST clock.
- Premium dashboard using backend-confirmed operational data only.
- Feature extraction started with `features/dashboard`.

Next:

- Complete feature-based shell extraction.
- Add full navigation architecture as functional routes become available.
- Build professional market strip, metrics, system-health, alerts, and table primitives from real API contracts.
- Add loading, empty, error, reconnection, and permission states.

## Phase 3 — Scanner workspace

- Data-quality states, sortable/filterable columns, saved views, setup inspector, rejected evaluations, and TradingView Lightweight Charts.

## Phase 4 — Strategy platform

- Formal registry interfaces, strategy definitions and versions, universe/session/risk settings, deterministic replay, ORB migration, and configuration snapshots on every signal.

## Phase 5 — Advanced paper trading

- Broker-independent paper order manager, fills, costs, slippage, partial fills, positions, P&L, and journal.

## Phase 6 — Risk engine

- Account, position, exposure, market, infrastructure, and session controls; transactional risk reservations; durable emergency stop.

## Phase 7 — Backtesting

- Historical data, deterministic runs, cost/slippage models, equity and drawdown analytics, strategy comparison, and look-ahead-bias tests.

## Phase 8 — OMS and execution core

- Immutable OrderIntent, unique idempotency keys, explicit order state machine including UNKNOWN, execution gateway, broker adapters, fills, positions, and reconciliation. LIVE remains disabled.

## Phase 9 — Shadow mode

- Real inputs and intended orders with zero broker submissions, plus paper/shadow comparison analytics.

## Phase 10 — Assisted trading

- Web/Telegram approval, callback authenticity and deduplication, expiry checks, and mandatory risk revalidation immediately before broker submission.

## Phase 11 — Live architecture

- Full automated lifecycle behind explicit compliance, broker, risk, health, reconciliation, static-IP, and administrator activation gates. No silent enablement.

## Phase 12 — Production hardening

- HTTPS reverse proxy, Windows home-PC deployment, security headers, monitoring, backups, recovery, resource limits, graceful shutdown, startup reconciliation, and secrets validation.

## Phase 13 — Final QA

- Complete backend, integration, load, recovery, browser, migration, Docker, dependency, and security gates.

## Phase 14 — Documentation

- Finalize architecture, security, trading safety, deployment, broker, risk, OMS, backtesting, Telegram, disaster-recovery, and Mermaid lifecycle documentation.

## Current validated checkpoint

- Backend: 31 tests passed.
- Python: Ruff lint and format checks passed.
- Frontend: ESLint passed.
- Frontend: Next.js production build passed.
- Browser: 8 Playwright journeys passed, including single-flight access-token refresh.
