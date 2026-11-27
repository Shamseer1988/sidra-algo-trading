# Phase 3 — Research execution isolation

Branch `claude/research-lab-phase3`, on top of Phase 2 (PR #27). **Not deployed.** Merge order: #25,
#26, #27, then this. Contains the one migration Shamseer approved for this phase (`0031_research_jobs`);
it has not been applied to production.

## Why

Backtests and sweeps ran inside the `api` process, in a thread. A thread keeps the event loop answering,
but a pure-Python replay holds the GIL, so everything else in that process slows down while a sweep
runs for an hour. The `api` process is also where the scheduler runs the per-minute live exit sweep,
submission recovery and reconciliation.

## What was built

| Piece | File | What it does |
|---|---|---|
| Job queue tables | `alembic/versions/0031_research_jobs.py`, `db/models.py` (`ResearchJob`, `ResearchJobEvent`) | One row per job (type, status, payload frozen at request time, result, attempts, timeout, cancel flag, heartbeat), and an append-only event per state change with its actor. Two new tables; nothing existing altered. Tested up, down and up again locally |
| Queue operations | `services/research_jobs.py` | `enqueue`; `claim_next` with `FOR UPDATE SKIP LOCKED` (two workers never take one job); `heartbeat` (reports a cancel request); `finish` (first outcome stands); `request_cancel`; `release`; `requeue_stale` (dead worker: re-queue while attempts remain, then fail); `wait_for`; `research_allowed` |
| Research window | `research_jobs.research_allowed` | Heavy work only outside **9:00 AM – 3:40 PM IST on trading days**; weekends and exchange holidays are open |
| Job execution | `services/research_runs.py` | The backtest and sweep logic moved out of the routes. Inputs come from the job payload (strategies, controls, charges frozen when asked); indicator periods are read at run time via `indicator_settings.effective`, as the scanner does. Rows written and SUCCEEDED commit in one transaction |
| Worker | `app/research_worker.py` | Loop: worker heartbeat to Redis, re-queue stale jobs, respect the window, claim, supervise. Each job runs in a **spawned child process**, so a timeout or cancel can terminate it. The parent keeps the job's heartbeat, enforces the timeout, and fails the job if the child dies without recording an outcome. On shutdown the job is put back in the queue |
| Cannot trade | `research_worker.refuse_broker_credentials`, `disable_order_paths`, Compose | Three independent locks: (1) Compose blanks all 14 broker and Telegram variables for this service, including `UPSTOX_TOKEN_ENCRYPTION_KEY`, so the stored Upstox token cannot be decrypted; (2) the worker exits at startup if any is set (the message names variables, never values); (3) `place_order`, `cancel_order`, `modify_order`, `send_prepared_order` and `submit_live_order` are replaced with functions that raise, in the worker and in every child |
| Compose service | `docker-compose.yml` `research-worker` | Same image, `cpus: 1.0`, `mem_limit: 1536m`, `pids_limit: 128`, internal network only (no edge, no ports), non-root, `cap_drop: ALL` |
| API routes | `api/routes/backtesting.py` | `/backtests/run`, the Builder's draft backtest and `/backtests/sweeps` now **enqueue and wait**. They contain no replay code (a test checks this). Inside the research window they answer at once with 409 "Queued as research job …". A backtest waits up to 10 minutes and a sweep 1 minute; after that they answer "still running as job …", and the result appears in the list when it finishes |
| Jobs API | `api/routes/research.py` | `GET /api/v1/research/jobs`, `GET /research/jobs/{id}` (with events), `POST /research/jobs/{id}/cancel` (ADMIN), `GET /research/worker` (heartbeat, window, queued and running counts) |
| Visibility | `features/system/system-health-panel.tsx` | A "Research worker" card on System health |
| Deploy check | `scripts/verify_deployment.py`, `CLAUDE.md` | `research-worker` is now one of the application services that must run this checkout |

## What it does not do yet (stated, not hidden)

* **Transitive imports.** Research code still imports `upstox_orders` and `broker_adapter` through
  `backtesting` → `paper_execution` → `live_shadow_runner`. Cutting that means changing the paper
  engine; the three locks above make it unusable from the worker instead.
* **Database grants.** The worker uses the same database role as the API. A read-only role on
  operational tables and a `research` schema (architecture doc, section 4) need production role changes,
  planned for Phase 4 with approval.
* **The entry-candle stop gap** from Phase 2 is not fixed here. It changes backtest numbers, so it goes
  with the Phase 4 equivalence tests, where the change can be measured.
* **UI.** Backtests now return a "queued" or "still running" message instead of a result when the
  worker is busy or the window is closed. A jobs screen is Phase 5.
* **Sweep requests** almost always answer "still running" (sweeps take longer than the 1-minute wait).
  The finished sweep appears in the sweep list.

## Tests

`tests/test_research_jobs.py` (28):
* queue order, `SKIP LOCKED` exclusivity, cancel, the first finish standing, stale re-queue then fail,
  release, `wait_for`;
* the research window at its edges, on a Saturday and on a holiday;
* credentials refused (names only), every order path raising, the Compose block blanking all 14
  variables with limits and no edge network;
* supervision with a fake process: timeout, cancel, crash without a result;
* the child recording success, an input error and an unknown type;
* **a real spawned child process** across the process boundary;
* the route queueing inside the window, the route returning the worker's result outside it, the routes
  containing no replay code, and only ADMIN being able to cancel.

Changed on purpose:
* `test_backtest_does_not_block::test_the_route_sends_both_replays_to_a_thread` is now
  `…_runs_no_replay_at_all`: the replay is no longer in the API at all.
* `test_backtest_correctness::…api_sweep_uses_the_stored_indicator_periods` now tests the sweep job in
  `research_runs`, where that code moved.
* `test_deployment_consistency`: the research worker is a required service.

### Full backend suite, this machine (Windows, Python 3.11, throwaway PostgreSQL 18, no Redis)

| Branch | Result |
|---|---|
| Phase 3 (this branch) | 2,795 passed, 16 failed |
| Phase 2 (`claude/research-lab-phase2`) | 2,760 passed, 16 failed |
| Failures only on Phase 3 | **none** — identical sets |

The 16 are the 15 Windows-environment failures listed in the Phase 2 report plus
`test_entry_race::test_without_the_lock_both_entries_go_through`, a timing test that failed on both
branches in this run and passed on the Phase 2 base in an earlier run. **`scripts/run-tests.sh` on the
NAS (Linux) is still required before any deploy.**

One real failure was found and fixed during this phase: the first version of the worker's lockdown
named `UpstoxOrderClient` and `FirstockOrderClient` to disable them, and
`test_submission_boundary` (only the gateway may name an order-capable client) refused it. The worker
now disables the gateway's factory, `live_order_adapter`, and the two send functions, including every
module's imported copy, and names no client class. The safety test was not changed.

Also caught before commit: a `ruff format app` run reformatted live-path files (`live_protection.py`,
`live_risk.py`, `scanner_orchestration.py`, `paper_execution.py` and others). They were restored from
git; this branch does not change them.

## Production actions needed (all need approval; none taken)

1. Merge #25 → #26 → #27 → this, then on the NAS `scripts/run-tests.sh`.
2. `alembic upgrade head` on production (adds two tables; safe under a running deployment).
3. Deploy with `research-worker` included:
   `SIDRA_BUILD_COMMIT=$(git rev-parse HEAD) docker compose up -d --build api scanner-worker swing-worker research-worker web`,
   then `scripts/verify-deployment.sh`.
4. Check the NAS has the memory for a 1.5 GB research container beside the others. Not measured here;
   lower `mem_limit` if needed. A sweep loads about 400 MB.

## Phase 4 plan (historical data and research storage), for approval

1. Measure provider limits: Upstox v3 historical 1-minute availability (the other session confirmed back
   to 2022-01 via the public endpoint, one month per request), rate limits, gaps.
2. A `research` schema and a `sidra_research` role: SELECT on operational market data, write only to
   `research.*`. Migration and production role creation; ask first.
3. `research.dataset` with source, range, instruments, row count and content hash;
   `research.dataset_coverage` per instrument and day (expected vs present bars, duplicates).
4. Backfill as a research job, after 3:40 PM, resumable, never writing `public.market_candles`. Reuse the
   other session's downloaded files (`~/sidra-research/data`) where provenance is recorded.
5. Instrument table with sector and tick size; NIFTY 200 membership by date for survivorship.
6. NSE holiday lists for the backfilled years (the calendar refuses unconfirmed years, so 2022–2025
   history cannot be stored without them).
7. Fix the entry-candle stop gap, with equivalence tests against recorded live days.
