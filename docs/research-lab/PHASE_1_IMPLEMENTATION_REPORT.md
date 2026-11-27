# Phase 1 — Implementation report

Branch `claude/research-lab-phase1`, from `a4548b0` on the shared line `claude/epic-knuth-g8brv1`.
Worktree `../sidra-research-lab`. **Not committed, not pushed, not deployed.**

## A. What was inspected

* Repository: `docker-compose*.yml`, `infra/docker/*.Dockerfile`, `infra/caddy/*`, `.github/workflows/ci.yml`,
  `scripts/run-tests.sh`, `CLAUDE.md`, `apps/api/app/core/config.py`, `apps/api/app/main.py`,
  `apps/api/app/api/deps.py`, routes `backtesting.py`, `settings.py` (strategy write path, `strategy_mode`),
  `strategy_builder.py`, `system.py`, `health.py`, `telegram.py`, `auth.py`; services `trading_calendar.py`,
  `scheduler.py`, `live_session_open.py`, `backtest_sweep.py`, `settings_history.py`, `upstox_auto_auth.py`,
  `upstox_orders.py`, `core/logging.py`; `scripts/backfill_history.py`; tests `test_strategy_builder.py`,
  `test_phase1_foundation.py`, `conftest.py`; web `system-health-panel.tsx`, `components/api.ts`,
  `lib/formatting.ts`, `features/backtesting/backtesting-workspace.tsx`.
* NAS (read-only): git state, deploy lock and log, containers and image ids, source hashes inside
  `api`, `scanner-worker`, `swing-worker`, `.env` variable names and non-secret values, Redis safety
  keys, `alembic_version`, `live_order_submissions`, `execution_reconciliations`, `live_activations`,
  table sizes, candle coverage, the Cloudflare tunnel's mounts and network mode (token redacted), DSM
  reverse-proxy targets. Three GET requests to the public webhook hostname.
* Details are in `PHASE_0_PREFLIGHT_REPORT.md`.

## B. What was changed

### 1. Sweep promotion guard — `apps/api/app/api/routes/backtesting.py`

* **Problem.** `promote_sweep_combination` wrote `paper_strategies` directly. It could change the LIVE
  RS Pullback with no confirmation, no reason and no `setting_revisions` row. It also copied any key in a
  stored combination that matched a model field, including `enabled`, `live_orders` and `id`.
* **Change.**
  * `promotion_refusal()` refuses a LIVE strategy outright (HTTP 409) and a missing one (404).
  * Only sweepable strategy parameters (`STRATEGY_PARAMS`) can be written; the mode is forced back to the
    strategy's own `enabled`/`live_orders`.
  * The candidate is validated by `StrategyConfiguration` before anything is written (422 on a bad value).
  * The write goes through `settings._persist_strategies`, the single strategy write path: version bump,
    `record_revision`, and an `AuditLog` row `backtest.sweep_promoted` carrying the strategy, its mode,
    the parameters and an optional `reason` query parameter.
* **Why it is safe.** It only refuses more and records more. ADMIN-only is unchanged. The UI already
  shows the error text of a refused promotion. No live configuration was touched.
* **Impact.** Promoting to a PAPER or OFF strategy works as before, now with a revision. Promoting to the
  LIVE strategy is no longer possible; the message points to Paper mode or the Builder.
  Combinations that vary only exit, control or indicator parameters were already refused and still are.

### 2. NSE calendar coverage — `trading_calendar.py`, `scheduler.py`, `routes/system.py`, web

* **Problem.** Only 2026 is built in. On 1 January 2027 every session fails closed with no prior
  warning. Separately, adding a year to `NSE_CALENDAR_CONFIRMED_YEARS` without its holidays made every
  weekday of that year tradeable, Republic Day included.
* **Official 2027 circular.** Not published at the time of writing (NSE issued the 2026 list on
  12 December 2025, circular NSE/CMTR/71775). Third-party 2027 lists disagree on Holi, Muharram,
  Id-ul-Fitr and Diwali Balipratipada. **No 2027 date was added.**
* **Change.**
  * `BUILT_IN_HOLIDAYS` registry (2026 only); `has_holiday_list()`, `year_is_usable()`.
  * A year now trades only if it is confirmed **and** has a holiday list (built in, or operator
    overrides for that year). Otherwise it fails closed with the reason "confirmed but has no holiday
    list". 2026 behaviour is unchanged (every 2026 date gives the same answer as before; tested).
  * `coverage(today)` → OK / WARNING (next year unusable and ≤ 45 days away) / CRITICAL (this year
    unusable). It reports and never changes a decision.
  * The 8:45 AM session-open Telegram message gets a "Calendar:" line while WARNING, sent even if
    session-open alerts are muted. CRITICAL needs no extra line: on those days the open refuses at the
    calendar step and that refusal is already the message.
  * `GET /api/v1/system/overview` (authenticated) gains `calendar` and `build`; System health shows an
    "NSE calendar" card and an "API build" card. `warning` now renders amber, not red.
* **Effect on production today.** From 17 November the window is open, so after the next deploy the
  morning message will say the 2027 calendar is missing until it is added.
* **To finish when NSE publishes:** add `NSE_2027_HOLIDAYS` from the circular to `BUILT_IN_HOLIDAYS`, set
  `NSE_CALENDAR_CONFIRMED_YEARS=2026,2027`, update `test_no_year_after_2026_is_built_in…`, deploy before
  31 December.

### 3. Deployment consistency

* **Problem.** The documented deploy rebuilt only `api scanner-worker`; the swing worker ran older code;
  nothing could show which commit a container runs.
* **Change.**
  * `apps/api/app/services/build_info.py` (standard library only): `build_commit()` reads
    `SIDRA_BUILD_COMMIT` and returns it only if it is a 7–40 character hex string;
    `source_fingerprint()` hashes the `app` package's `.py` files (paths and bytes, `__pycache__`
    excluded). `python -m app.services.build_info` prints those two fields.
  * `apps/api/scripts/verify_deployment.py` + `scripts/verify-deployment.sh`: runs on the NAS host
    (Python 3.8), fingerprints the checkout, asks each of `api`, `scanner-worker`, `swing-worker` (source
    + commit) and `web` (commit) what it runs, and prints a table. FAIL (exit 1) on a different source, a
    different commit, or a service that is not running; WARN when the code matches but no commit is
    stamped (every image built before this change). Containers built before `build_info.py` existed
    are read through an inline fallback that computes the same fingerprint (a test holds the two equal).
    It never prints stderr and never reads `.env`.
  * `api.Dockerfile`, `web.Dockerfile`: `ARG/ENV SIDRA_BUILD_COMMIT` (after the installs, so only one
    layer rebuilds per commit). `docker-compose.yml`: the build argument on all four services.
  * `CLAUDE.md`: deploy command rebuilds all four services with the commit stamped, followed by the
    verification; service list corrected; deploy-lock protocol recorded.
* **Why it is safe.** Nothing changes at runtime until a deploy. The new code only reads files and one
  environment variable. The overview's build card is behind authentication.

### Files

| File | Change |
|---|---|
| `apps/api/app/api/routes/backtesting.py` | promotion guard |
| `apps/api/app/services/trading_calendar.py` | holiday registry, usable-year rule, coverage |
| `apps/api/app/services/scheduler.py` | `calendar_note()`, warning line in session-open alert |
| `apps/api/app/api/routes/system.py` | `calendar` and `build` in overview |
| `apps/api/app/services/build_info.py` | new |
| `apps/api/scripts/verify_deployment.py`, `scripts/verify-deployment.sh` | new |
| `infra/docker/api.Dockerfile`, `infra/docker/web.Dockerfile`, `docker-compose.yml` | build commit |
| `apps/web/features/system/system-health-panel.tsx`, `components/api.ts`, `lib/formatting.ts` | two cards, types, amber warning |
| `CLAUDE.md` | deploy section |
| `apps/api/tests/test_sweep_promotion_guard.py`, `test_calendar_coverage.py`, `test_deployment_consistency.py` | new tests |
| `docs/research-lab/*.md` | four documents |

## C. Testing

Environment: Python 3.11.6 virtualenv in the session scratchpad; a throwaway PostgreSQL 18 cluster on
`127.0.0.1:55432` (trust auth, database `sidra_test`), migrated to `0030`. No Redis (not needed by these
tests). Never pointed at the NAS.

| Run | Result |
|---|---|
| New tests (3 modules) | **46 passed** |
| Promotion tests against the **old** route code | 10 of 11 fail (as they should); new code restored |
| Related existing modules: strategy builder, strategy modes, settings routes, phase-1 foundation (calendar), backtesting, sweeps, shared pass, scheduler recovery, health, live session open, universe refresh time, worker startup | **133 passed** |
| Every other module touching the calendar or system routes (exit quality, history routes, live exit manager, live fills, sweep exits/cutoff, trade history, inspect-live-day labels) | 160 passed, **1 failed — pre-existing**: `test_inspect_live_day_labels::test_today_is_not_sent_to_the_backfill_script` fails identically on the unchanged branch (depends on today's date and an empty database) |
| Verification logic under the NAS host's Python 3.8.15 (code fed through stdin, fake Docker; nothing written to the deployment, one temporary file in `/tmp`) | ran correctly, flagged a stale swing worker |
| `ruff check` / `ruff format --check` on changed files | pass. Six pre-existing findings in untouched files |
| Web: `tsc --noEmit`, `eslint` on changed files, `next build` | pass |

Not run: the full backend suite, the Playwright suite, `scripts/run-tests.sh` on the NAS (would build
the `api` image while a sweep runs there), and `verify-deployment.sh` against the real containers. CI
will not run on a PR into `claude/epic-knuth-g8brv1` (CI triggers on `main` only).

Limitation: locally installed FastAPI is 0.165; the admin-role test reads the route from the router
rather than the app because newer FastAPI nests included routers. Production's FastAPI version was not
checked.

## D. Safety verification

| Statement | Confirmed |
|---|---|
| No broker order was submitted, modified or cancelled | yes — no broker endpoint was called |
| No live strategy setting, risk limit, capital or credential was changed | yes |
| Application mode not switched | yes |
| No production migration | yes (none written) |
| No production container built, restarted, recreated or stopped | yes |
| No historical data deleted or modified | yes |
| No secret printed | yes — names only; the tunnel token was redacted before display |
| Live exits, reconciliation, recovery, protection code untouched | yes — no `live_*`, `upstox_orders`, `broker_adapter` file changed |
| The other session's checkout and PR #25 files untouched | yes — separate worktree; no shared file |

## E. Outstanding items

* **Deploy (needs approval).** Merge this branch into `claude/epic-knuth-g8brv1` after PR #25 (or rebase
  onto it), run `scripts/run-tests.sh`, then the four-service deploy and `scripts/verify-deployment.sh`.
  Outside market hours, holding the deploy lock, not while a sweep runs in `api`. The first deploy will
  rebuild all four images (new build argument).
* **2027 calendar** when NSE publishes, before 31 December.
* Security register S1–S10.
* CI covers PRs into `main` only.
* Pre-existing: the date-dependent test; six lint findings; `pg_stat` counters reading zero.

## F. Phase 2 plan — backtesting correctness

Inputs: the audit, and the other session's 5-year SIP study (public Upstox data, NIFTY 200, 2022-01 to
2026-11, 5-minute and daily on the NAS host at `~/sidra-research/data`; 1-minute available from the same
API). That study showed the shape of the problem: SIP v1's best 2022–23 settings all failed in 2024–26.

1. **Sweep honesty.** Remove `max_trades_per_day` and `cooldown_minutes` from `STRATEGY_PARAMS` until the
   simulator honours them (item 3), so no sweep claims to test them. Make the API sweep use
   `effective_settings()` like the single backtest; check `scripts/run_sweep.py` the same way.
2. **Time exits.** Apply the account square-off (15:00) and each strategy's hold limit in `_exit_trade`,
   using `exit_rules.time_exit_due`, at the first candle at or after the deadline, filled at its open.
3. **Portfolio-day simulator** (new module, existing replay untouched until it is replaced): candidate
   signals from the existing replay, then one pass per session in time order applying account,
   per-share, per-sector (`sectors.py`), per-strategy and per-side ceilings, cooldown, maximum open
   positions, exposure cap with leverage, daily loss stop and profit target, with the same rules as
   `trade_counter` and `daily_limits` (shared functions, not copies).
4. **Fills.** Entry at the next candle open with slippage (exists). Stop at the worse of the stop and the
   candle open when the candle opens through it. Same-candle stop and target: stop first (exists).
5. **Charges.** Reuse `paper_execution.transaction_costs` (brokerage, STT, exchange, SEBI, stamp, GST);
   add a cost-sensitivity multiplier.
6. **Selection.** Rank on in-sample only; report the validation block without ranking on it; record the
   number of variants; add walk-forward folds (rolling train/test) as a mode.
7. **Equivalence tests.** Fixed candle fixtures where the expected trades are written by hand; one
   replay of a recorded live day compared with what live actually did.
8. **First job once correct:** RS Pullback over 2022–2026 on 1-minute data with walk-forward, which has
   never been done. RS Pullback is the only strategy trading real money.

Phase 2 needs no migration if results stay in `backtest_*` JSON; the research schema waits for Phase 4.

**Stopped here. Phase 2 has not been started.**
