# Research Lab architecture

Design only. Nothing in this document is built yet except where it says "exists". Phases 2–6 are
built one at a time, each after review.

## 1. Principle

The Research Lab is a second, separate capability inside the same application. It reads market data
and strategy rules. It never places, cancels or modifies an order, never writes live strategy settings
or risk limits, and never touches scanner controls, exits or reconciliation. Its output is a
recommendation. Turning a recommendation into a setting is a human action through the existing strategy
mode and Builder screens, which ask for confirmation and record a revision.

## 2. Boundary

```text
                       EXISTING SIDRA (unchanged)                         RESEARCH LAB (new)
  ┌──────────────────────────────────────────────────────┐   ┌───────────────────────────────────────┐
  │ Upstox V3 feed ─► scanner-worker                      │   │                                       │
  │   candles + indicators ─► market_candles (public)     │──►│ nightly copy (after 3:40 PM)          │
  │   strategies (StrategyRegistry.evaluate, plan_trade)  │   │   ─► research.candles (versioned)      │
  │   ─► paper OMS / live_entry_bridge ─► Upstox orders   │   │                                       │
  │ api: REST, exit sweep, reconcile, recovery, open      │   │ api: research routes ─► research.jobs  │
  │ swing-worker (paper)                                  │   │        (enqueue + read only)           │
  │ settings: application_settings, setting_revisions     │   │ research-worker (own container)        │
  └──────────────────────────────────────────────────────┘   │   ─► simulator (reuses strategy code)  │
           ▲                                                   │   ─► validation ─► reports            │
           │ only through Strategies › Builder / mode switch    │ research.* tables only                 │
           └────────── human, with confirmation and reason ◄────┴───────────────────────────────────────┘
```

## 3. Workers and responsibilities

| Process | Today | Research Lab change |
|---|---|---|
| `api` | REST, APScheduler (exits every minute, recovery, reconcile, session open) and, today, backtests and sweeps in a thread | Research routes only enqueue jobs and read results. Backtest and sweep execution move out (Phase 3) |
| `scanner-worker` | Feed, candles, signals, live entries | Unchanged |
| `swing-worker` | Swing paper desk | Unchanged |
| `research-worker` (new, Phase 3) | — | Claims jobs from `research.jobs`, runs them with CPU and memory limits, writes results to `research.*`. Heartbeat, timeout, cancellation, retry |
| `web` | UI | New Research workspace (Phase 5) |

The research worker gets its own Compose service with `cpus` and `mem_limit`, `restart: unless-stopped`,
no broker credentials in its environment (a separate `env_file` without `UPSTOX_*`, `FIRSTOCK_*`,
`TELEGRAM_BOT_TOKEN`), and the same internal network.

## 4. Database permissions

| Role | Schema `public` (operational) | Schema `research` |
|---|---|---|
| `sidra_app` (existing) | read/write | read (to show results in the UI), write `research.jobs` (enqueue, cancel) |
| `sidra_research` (new) | **SELECT only** on `market_candles`, `market_indicator_snapshots`, `corporate_events`, `scan_universe`, `application_settings` (to snapshot current rules), `backtest_*` (legacy, read) | read/write |

`sidra_research` has no grant on `live_order_submissions`, `live_*`, `paper_*`, `oms_*`, `session_halts`,
`users`, `user_sessions`, `broker_credentials`, `setting_revisions` writes, or `application_settings`
writes. Enforced by Postgres, not by code. Creating the role and schema needs a migration and production
approval.

## 5. Research entities (Phase 4–5)

| Table | Holds |
|---|---|
| `research.dataset` | id, name, source (Upstox API, NSE file, public 5-min download), timeframe, instruments, date range, row count, content hash, created_at, provenance notes |
| `research.dataset_coverage` | per dataset × instrument × session: bars expected, bars present, duplicates, gaps |
| `research.instrument` | token, symbol, sector, tick size, listing dates |
| `research.universe_membership` | index name, token, valid_from, valid_to (for survivorship-free universes) |
| `research.hypothesis` | statement, rationale, expected effect, pre-registered metric and threshold, status |
| `research.feature_definition` | name, formula reference, inputs, version, code hash |
| `research.strategy_version` | strategy type, parameters, implementation code hash (`build_info.source_fingerprint` of the strategy modules), created_at |
| `research.experiment` | hypothesis, dataset, strategy_version, grid, execution assumptions (slippage, charges, fills), date ranges per split, number of variants, status |
| `research.experiment_run` | one variant: parameters, split, metrics, started/finished, worker, job id |
| `research.backtest_trade` | per trade: run, instrument, times, prices, quantity, gross, costs, net, R, exit reason, feature snapshot |
| `research.walk_forward_fold` | train range, test range, chosen parameters, out-of-sample metrics |
| `research.validation_result` | test name (bootstrap CI, deflated Sharpe, cost sensitivity, regime split), value, pass/fail |
| `research.report` | experiment, conclusion, recommendation (never an action), author, created_at |
| `research.jobs` | type, payload, status, priority, attempts, heartbeat, timeout, cancel_requested, result reference |

Every experiment row records dataset id and hash, strategy implementation hash, parameters, indicator
settings, date range, universe, execution and cost assumptions, number of variants tested, results and
validation status. The final holdout range is stored on the experiment and the worker refuses to score
it until the experiment is marked "selection finished".

## 6. Data flow

1. Operational candles stay in `public.market_candles`. Nothing is deleted.
2. After market close a job copies new candles into a versioned `research` dataset and writes coverage.
3. Long-history backfills (Phase 4) run after 3:40 PM, respect Upstox rate limits, and write only to
   `research` tables. Each instrument and day reports coverage; missing data is reported, never filled
   in.
4. Jobs read a dataset by id, never "whatever is in the table now", so a result can be reproduced.

## 7. Job execution

* The API inserts a job row and returns its id. The worker claims it with
  `SELECT … FOR UPDATE SKIP LOCKED`, sets a heartbeat every few seconds, and honours `cancel_requested`.
* A job past its timeout is marked failed; a job whose worker heartbeat stops is returned to the queue
  once, then failed.
* The worker never runs between 9:00 AM and 3:40 PM on trading days unless a job is explicitly marked
  light (read-only reports), so it cannot compete with the live scanner for CPU or memory.
* Audit: every job state change is a row; who enqueued it is recorded.

## 8. Strategy-version traceability

The simulator calls the same `StrategyRegistry.evaluate` and `plan_trade` the scanner uses. A run records
the source fingerprint of the strategy and exit modules it ran with. A live trade records the strategy
version and the stored configuration snapshot (exists today in `paper_signals.strategy_snapshot`). A
research result can then be matched to the live behaviour it claims to predict, or shown not to match.

## 9. Security boundaries

* No broker client is importable in a meaningful way from the research worker: it has no credentials,
  and a test asserts the research modules import nothing from `live_*`, `upstox_orders`,
  `broker_adapter` or `firstock.orders`.
* Database grants as in section 4.
* Promotion stays human. The promotion guard added in Phase 1 already refuses to write a LIVE strategy
  from a sweep; Phase 5 removes promotion from research screens entirely and links to the Builder.

## 10. Phase order

2. Backtest correctness (portfolio-day simulator, ceilings, square-off, gap fills, effective settings).
3. Research worker and job queue; move execution out of the API.
4. Research schema, datasets, coverage, backfill.
5. Research UI modules.
6. Strategy discovery, only through pre-registered experiments with a sealed holdout.
