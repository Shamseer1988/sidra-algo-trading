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

## Phase 1 — Safety foundation (complete)

Completed:

- CI now targets `main` pushes and pull requests.
- Market ticks retain exchange and received timestamps; candle bucketing uses exchange time and latency is tracked separately.
- Persisted enabled strategy configurations now drive scanner evaluation with per-version state and signal keys.
- API requests use single-flight refresh and retry once; unrelated server failures no longer force login redirects.
- Scanner WebSockets use same-origin `ws/wss`.
- FastAPI is internal-only in Compose; API, worker, and web containers run non-root with dropped capabilities.
- Unsupported Upstox browser-login automation was removed; Firstock's supported server-side TOTP dependency remains.
- Authoritative encrypted Telegram configuration remains shared across status, notifications, and callbacks.
- The fail-closed NSE Trading Calendar distinguishes weekends, official holidays, pre-open, regular, post-market, and operator-configured special sessions.
- Per-instrument data quality tracks expected/received bars, missing buckets, freshness, latency, duplicate ticks, and out-of-order ticks. `INVALID` and `STALE` snapshots block signals.
- The scanner supervises market-data tasks with bounded exponential restart backoff, durable health detail, restart counts, and recovery from iteration-level infrastructure failures.
- Production startup validation rejects insecure cookies, HTTP browser origins, schema auto-creation, placeholder JWT secrets, and malformed calendar configuration.

Production reverse proxy, TLS termination, resource limits, startup reconciliation, and operational monitoring remain Phase 12 work.

## Phase 2 — Premium terminal UI (complete)

Completed:

- Sidra Algo visual identity and command-center information hierarchy.
- Semantic Light, Dark, and System themes with persisted preference and no initial theme flash.
- Compact responsive sidebar with collapsed icon mode and mobile drawer.
- Global header with real scanner/feed state, PAPER badge, and live IST clock.
- Premium dashboard using backend-confirmed operational data only.
- Replaced the monolithic terminal shell with modular layout, navigation, shared formatting, and feature workspaces.
- Added the complete information architecture: trading, risk and analytics, brokers, automation, and system sections with responsive drawer, collapsed navigation, active state, and planned-workspace notices.
- Added real-data Market, Scanner, System Health, Journal, broker configuration, audit, controls, signals, strategies, and settings workspaces.
- Added explicit unavailable states for order, position, performance, backtesting, automation, scheduler, and user-management areas; no placeholder trading data is presented.
- Added shared responsive toolbar, table, status, data-list, skeleton, and empty-state primitives.
- Unified glassmorphism system across navigation, headers, cards, forms, tables, status badges, authentication, and broker callback states in both light and dark themes.

## Phase 3 — Scanner workspace (complete)

- Added a durable, idempotent scanner-evaluation audit record for accepted, watching, rejected, and data-quality-blocked completed-candle decisions.
- Added Alembic migration `0008_scanner_evaluations`; also fixed Alembic handling for valid percent-encoded database URLs.
- Added authenticated scanner-evaluation APIs and real-time event notifications.
- Built a professional scanner workspace with search, sorting, state and quality filters, persisted device-local saved views/watchlist, data-quality strip, responsive evaluation tape, and setup inspector.
- The inspector shows only recorded completed candles and strategy output: score breakdown, conditions, failures, proposed paper entry/stop/target/quantity/risk, and reward:risk. It does not imply a broker order or position.
- Added backend coverage for evaluation classification and browser coverage for rejected-setup filtering and inspection.

## Phase 4 — Strategy platform (complete)

- Added a formal, versioned `StrategyRegistry` and deterministic ORB Retest definition; unsupported strategy implementations cannot be persisted.
- Strategy configuration now covers enabled state, universe, session policy, allowed sides, score/reward:risk/indicator thresholds, per-strategy risk, daily paper-trade cap, and cooldown.
- Every scanner evaluation and paper signal stores its immutable strategy configuration plus the effective trading-control snapshot; Alembic migration `0009_strategy_snapshots` is applied.
- Strategy configuration changes increment a version, while strategy-specific state and signal keys remain versioned and deterministic.
- Added acceptance metrics and a glassmorphism strategy workspace with safe administrator controls and read-only RBAC behavior.
- Added deterministic registry coverage and browser coverage for the strategy workspace.

## Phase 5 — Advanced paper trading (complete)

- Added a broker-independent, paper-only order manager with no broker identifier, credential, or submission path.
- Added durable paper orders, immutable fills with complete fee breakdowns, and signal-linked paper positions through Alembic migration `0010_paper_execution`.
- Simulated execution is deterministic and uses only subsequent completed candles, bounded participation for partial fills, configurable slippage, and configurable Indian equity transaction-cost components.
- Added simulated market, limit, and stop order handling; OCO-style target/stop brackets conservatively prioritize the stop when one completed candle touches both levels.
- Added mark-to-market realized/unrealized/net P&L, fees, order lifecycle, positions, paper-execution APIs, and premium glassmorphism Orderbook and Positions workspaces.
- Kept scanner outcomes and the CSV journal intact while explicitly separating them from the simulated execution ledger.

## Phase 6 — Risk engine (complete)

- Added transactional, database-backed paper-risk reservations with a PostgreSQL advisory lock to prevent concurrent scanner workers from over-allocating daily risk.
- Enforced daily allocation, maximum open positions, and maximum open exposure before any simulated entry order is queued; rejections are retained with their decision reason for auditability.
- Settled reservations when a paper position closes while retaining that allocation in the day’s risk budget, and exposed verified allocation, capacity, and exposure through the Risk API.
- Added a premium glassmorphism Risk Center and browser coverage for its live reservation-capacity metrics.

## Phase 7 — Backtesting (complete)

- Added a durable historical-research ledger through Alembic migration `0012_backtesting`, preserving immutable run inputs, strategy/control/cost snapshots, a benchmark-inclusive data fingerprint, and trade-level results.
- Added completed-candle-only replay: decisions only see bars that have closed, entries use the next candle, stop/target ties favor the protective stop, and the existing paper slippage and Indian-equity cost model is reused.
- Added net P&L, win rate, profit factor, closed-trade equity, drawdown, and per-strategy comparison analytics with protected API access.
- Added a premium glassmorphism Backtesting Lab and deterministic/look-ahead regression coverage.

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

- Backend: 51 tests passed.
- Python: Ruff lint and format checks passed.
- Frontend: ESLint and Next.js production build passed.
- Browser: 13 Playwright journeys passed, including Risk Center and Backtesting Lab research workflows.
