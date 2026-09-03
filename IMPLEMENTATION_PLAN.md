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

## Phase 8 — OMS and execution core (complete)

- Added immutable paper-only `OrderIntent` records with unique idempotency keys, linked OMS orders, append-only lifecycle events, and an explicit `UNKNOWN` state through Alembic migration `0013_oms_core`.
- Linked scanner-queued paper entries and subsequent paper fills into the OMS gateway while preserving the existing simulation ledger; no OMS path calls a broker SDK or writes a broker order identifier.
- Added bounded paper reconciliation checkpoints that flag unknown or unlinked OMS orders for review, together with protected OMS APIs and a premium glassmorphism operations workspace.
- Added lifecycle/idempotency coverage and retained `LIVE_TRADING_ENABLED=false` throughout.

## Phase 9 — Shadow mode (complete)

- Added a zero-submission shadow ledger through Alembic migration `0014_shadow_mode`, automatically capturing intended paper OMS entries from real scanner and market-data inputs.
- Compared intended entry prices with completed paper fills, retaining per-order deltas and summary analytics while reporting an explicit zero broker-submission count.
- Added protected Shadow Mode APIs and a premium glassmorphism comparison workspace; no shadow path imports or calls a broker SDK.

## Phase 10 — Assisted trading (complete)

- Added protected, administrator-only web decisions and authenticated Telegram approval callbacks, both written to a durable approval ledger.
- Telegram webhook secret validation, sender/chat allow-list enforcement, unique inbound-event records, and terminal decision semantics prevent untrusted or replayed callback effects.
- Approval decisions expire deterministically and every approval revalidates the paper risk ledger immediately before the paper-only decision is recorded.
- Added an explicit broker-submission boundary: approvals can only reach `APPROVED_PAPER_ONLY`, where the system records that live broker submission is unavailable.
- Added the premium glassmorphism Assisted Trading workspace, service-level regression coverage, and an assisted-approval browser journey.

## Phase 11 — Live architecture (complete, hard-locked)

- Added an immutable live-readiness review ledger through Alembic migration `0016_live_readiness`, with administrator-audited review history.
- Added a protected readiness API and premium glassmorphism Live Gates workspace that evaluates runtime locking, compliance, static egress IP, service health, broker adapter, live risk, reconciliation, and administrator activation prerequisites.
- The readiness service hard-codes `overall_ready=false` and `live_execution_available=false`; no review, setting, or web action can enable trading or submit a broker order.
- Added regression and browser coverage proving the hard execution boundary is retained.

Live release remains unavailable by design: broker-approved submission, a separate live risk engine, external reconciliation, compliance, verified static IP, and a separately authorized activation must all exist and pass the gate before a future live release can be considered. No silent enablement.

## Phase 12 — Production hardening (in progress)

Completed foundation:

- Added process liveness probes, dependency-aware web startup, restart-safe init processes, graceful stop windows, and PID limits to the Compose topology.
- Added strict API CSP, clickjacking, MIME-sniffing, referrer, permissions, cross-origin isolation, and production-only HSTS headers with regression coverage.
- Validated the Compose file and retained loopback-only web/database/Redis bindings; no public service port was introduced.
- Added dependency-aware readiness probes, a safe startup paper-OMS reconciliation checkpoint, and backup/restore scripts that use the configured database identity and fail closed on restore errors.
- Added a validated, opt-in Caddy HTTPS overlay with automatic HTTP-to-HTTPS redirects, WebSocket-compatible reverse proxying, durable certificate storage, and required DNS/ACME environment values; it is not part of the default local stack.
- Added reconciliation observability to the glassmorphism System Health workspace, exposing the latest durable OMS checkpoint and its review state to operators.

Remaining:

- HTTPS reverse-proxy and certificate deployment, Windows deployment runbook, monitoring/backup/recovery operations, resource sizing, startup reconciliation, and production secret rotation. These require the target domain, network, and operator infrastructure choices.

## Audit follow-up — P0 signal-quality fixes (in progress, started 3 September 2026)

A full-application audit found the scanner could not reliably produce signals with the default
configuration. P0 corrections:

- **Signal path crash.** `PaperScannerOrchestrator` raised `UnboundLocalError` on `latest` whenever
  a strategy reached `SIGNALLED` with `cooldown_minutes = 0` (the default). The quota check is now
  `_signal_block_reason`, evaluated per SIGNALLED decision, with a safe default.
- **Score ceiling.** `market_confirmation` scoring needs the NIFTY benchmark; when the index was not
  streamed the maximum score was 80 while the strategy default `minimum_score` was 90, so no signal
  could ever fire. `StrategyConfiguration.minimum_score` now defaults to 80 (matching
  `DEFAULT_TRADING_CONTROLS`), and the live feed always subscribes the benchmark
  (`feed_subscriptions` for Upstox and Firstock) with a `scanner.benchmark_snapshot_missing` warning.
- **Universe starvation.** The account-wide `maximum_signals` limit short-circuited the whole
  completed-candle handler, so once the ceiling was hit no further instruments were evaluated or
  recorded. Evaluations are now always recorded; only signal creation is gated, through an ordered
  set of limits: daily ceiling → per-strategy `max_trades_per_day` → optional per-strategy
  `max_trades_per_side` → per-strategy cooldown.
- **Batch isolation.** Each strategy evaluation for a completed candle runs in its own guarded
  scope so one failing instrument cannot abort the batch or the market-data task.
- **Backfill visibility.** Providers without intraday historical backfill (Firstock) now log
  `scanner.no_intraday_backfill`; the data-quality gate already fails closed until session data is
  complete.

Coverage: `tests/test_market_data_subscriptions.py`, strategy-default assertions in
`tests/test_paper_strategy.py`, and a database-backed `_signal_block_reason` regression in
`tests/test_phase1_foundation.py`.

## Audit follow-up — P1 signal-quality work (in progress, started 3 September 2026)

### P1-a — Volatility and structure indicators (complete)

- Added Wilder `atr`, `atr_percent`, volume-weighted `vwap_bands` (VWAP ±1σ/±2σ), `opening_range_atr`
  (opening-range width in ATR units), and `extension_atr` (distance of close from VWAP in ATR units)
  to `market_calculations`, and threaded them through `indicator_snapshot`, the live candle pipeline,
  and the backtester. New `ATR_PERIOD` setting (default 14). Pure-function coverage added.
- These fields are stored in every `ScannerEvaluation.indicator_snapshot` and are the inputs the
  next scoring and universe-ranking passes consume.

### P1-c — Volatility-aware stops and exposure-bounded sizing (complete)

- `_risk_plan` now sets the stop to the widest of the structural distance, an ATR multiple
  (`stop_atr_multiple`, default 1.1), and a minimum percent of price (`min_stop_distance_percent`,
  default 0.35%). This removes the sub-tick "noise" stops that `retest_tolerance_percent` alone
  produced.
- Quantity is bounded by both the per-trade risk budget and the simulated account exposure ceiling
  (`account_capital × maximum_open_exposure_percent × leverage`), so a signal is no longer created
  only to be discarded by the risk engine for exceeding exposure.
- Both new controls are editable from the settings workspace.

### P1-b — Continuous weighted scoring (complete)

- `_score` no longer returns binary 5×20 with an always-granted 20 for `breakout_retest`. Each of the
  five components is now bounded partial credit (0-20), deterministic, still summing to 0-100 and still
  keyed the same way so stored breakdowns and the failed-condition list are unchanged:
  - `breakout_retest`: base credit plus reclaim strength beyond the level in ATR units, scaled down
    when the opening range is a small fraction of ATR (no real range).
  - `ema_alignment`: EMA direction stays a hard gate; separation beyond the choppy threshold is a bonus.
  - `vwap_alignment`: wrong side of VWAP is still a hard zero; otherwise graded by VWAP-band position so
    an overextended entry scores lower than one just above VWAP; capped when `extension_atr` is large.
  - `volume_confirmation`: below the configured RVOL multiple is a hard zero; above it scales continuously
    to full at twice the multiple.
  - `market_confirmation`: NIFTY regime agreement plus relative-strength magnitude in the trade direction.
- Missing inputs (no ATR yet, no benchmark snapshot, no volume baseline) award the affected component
  full credit rather than zero, so scoring only tightens selection when the data is actually present.

### P1-a2 — Daily history and time-of-day volume (complete)

- On Upstox startup the worker also fetches recent completed daily candles (`backfill_daily_history`,
  `DAILY_HISTORY_SESSIONS` default 40), stored in `market_candles` with `timeframe_seconds = 86400`.
  The live 1-minute pipeline never reads that timeframe, so the two histories stay isolated.
- `indicator_snapshot` gained `prior_day` (close/high/low), `gap_percent`, `daily_atr`,
  `daily_atr_percent`, `distance_to_prior_high_atr`, `distance_to_prior_low_atr`, and `time_of_day_rvol`
  (latest bar volume vs the average at the same IST minute over the last `RVOL_BASELINE_SESSIONS`
  sessions — activates once that 1-minute history has accumulated).
- The persistence service loads the daily candles and builds the time-of-day baseline once per
  instrument per session (cached). The backtester derives the same daily candles from prior sessions
  in its input, so live and historical evaluation stay on identical inputs.

### P1-d — Dynamic scan universe (complete, opt-in)

- New `scan_universe` table (Alembic `0017_scan_universe`) and `ScanUniverseEntry` model holding a
  per-session ranked candidate list.
- `services/universe.py` — a pure, deterministic `rank_universe` that screens the streamed
  instruments on hard gates (daily history, liquidity floor, price band, ATR% band) and scores the
  survivors on a liquidity / volatility / gap / momentum composite, plus `refresh_universe` which
  reads persisted daily candles and today's first minute and upserts the result.
- The scanner worker rebuilds the universe once per session after `UNIVERSE_REFRESH_TIME` (Upstox
  only). `PaperScannerOrchestrator` skips instruments outside the selected set — but only when
  `UNIVERSE_ENABLED` is true and the universe has been built; it fails open otherwise, so existing
  deployments see no behaviour change until they opt in.
- Protected `/universe`, `/universe/summary`, and admin `/universe/refresh` APIs, and a "Scan
  universe" workspace showing the ranked selection, component scores, and screened-out candidates.
- Settings: `UNIVERSE_ENABLED` (default false), `UNIVERSE_SIZE`, `UNIVERSE_REFRESH_TIME`, and the
  liquidity / price / volatility gate bounds.

### P1-e — Composite market regime (complete, opt-in)

- Pure functions in `market_calculations`: `india_vix_state` (calm / normal / stressed / extreme),
  `market_breadth` (fraction of tracked instruments above their VWAP), and `compose_market_regime`
  which blends intraday NIFTY structure, NIFTY vs prior close, India VIX and breadth into a
  RISK_ON / NEUTRAL / RISK_OFF regime with a 0-100 score, `allow_long` / `allow_short` gates, and a
  `size_multiplier`.
- `services/market_regime.py` keeps a per-session breadth marker in Redis (updated from every equity
  snapshot) and, on each benchmark candle, reads the India VIX tick and the NIFTY snapshot and stores
  `market:regime`. `indicator_snapshot` now also exposes `last_close`.
- `PaperScannerOrchestrator` reads the regime and, when `MARKET_REGIME_ENABLED`: scales
  `risk_per_trade_percent` by the size multiplier before evaluation and downgrades a signal whose
  side the regime disallows (recorded on the evaluation with the reason). India VIX is force-added to
  the Upstox feed when the regime is enabled.
- Protected `GET /market-data/regime` and a regime card on the Market workspace (score, allowed
  sides, size multiplier, VIX level/state, breadth).
- Settings: `MARKET_REGIME_ENABLED` (default false), `UPSTOX_INDIA_VIX_KEY`, and the VIX
  calm/stressed/extreme thresholds. Also fixed two pre-existing Ruff errors in
  `api/routes/market_data.py` while editing it.

### P2 (in progress)

- **Symbol resolution & alert accuracy (done).** `trading_symbols` gained `resolve_script_names` /
  `resolve_symbol`, which fall back to the persisted Upstox instrument master when the static table
  and key parsing cannot name an instrument. Scanner signal/evaluation and universe API responses now
  carry a `script_name`; the frontend prefers it. The Telegram signal alert uses the configured
  intraday leverage multiplier instead of a hard-coded 5x.
- **Additional strategies (done).** `paper_strategy.plan_trade` is now the shared, volatility-aware
  entry/stop/target/quantity helper (ORB delegates to it). Two new deterministic state machines in
  `extra_strategies.py`:
  - **VWAP Pullback** (`vwap-pullback-v1`) — a controlled pullback to session VWAP inside an
    EMA-defined trend, entered on the reclaim candle.
  - **EMA Momentum** (`ema-momentum-v1`) — a fresh push through the opening range with a stacked
    EMA / VWAP trend, rejected when already extended from VWAP.
  Both are registered in `StrategyRegistry`, share the versioned `StrategyConfiguration`, and are
  selectable in the Strategies workspace (new `/settings/strategies/definitions` API, strategy-type
  dropdown). Deterministic coverage in `tests/test_extra_strategies.py`.

## Phase 13 — Final QA

- Complete backend, integration, load, recovery, browser, migration, Docker, dependency, and security gates.

## Phase 14 — Documentation

- Finalize architecture, security, trading safety, deployment, broker, risk, OMS, backtesting, Telegram, disaster-recovery, and Mermaid lifecycle documentation.

## Current validated checkpoint

- Backend: 57 tests passed.
- Python: Ruff lint and format checks passed.
- Frontend: ESLint and Next.js production build passed.
- Browser: 16 Playwright journeys passed, including the restart-reconciliation monitoring journey.
