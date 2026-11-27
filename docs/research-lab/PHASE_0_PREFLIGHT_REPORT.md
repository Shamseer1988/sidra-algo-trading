# Phase 0 — Preflight report

Date: Friday 27 November 2026, from about 8:15 PM to 9:30 PM IST (after market close).
Method: read-only. On the NAS: `git`, `docker compose ps`, `docker inspect`, file hashes inside
running containers, the allow-listed read-only scripts `audit_deployment.py`, `check_settings.py` and
`candle_coverage.py`, Redis `GET`/`HGETALL`, and `SELECT` statements. No broker endpoint was called. No
container was built, restarted or recreated. No setting, row or file in the deployment was changed.

## 1. Source and deployment versions

| Item | Value |
|---|---|
| Shared deploy line | `claude/epic-knuth-g8brv1` (not `main`; `origin/main` is at `8157090`, far behind) |
| NAS checkout | `a4548b0` on `claude/epic-knuth-g8brv1` (merge of PR #24). Untracked: `caddy-root.crt`, `reports_selection_candidates_20261120.json`, `setup.txt` |
| NAS deploy lock | none held. Log: PR #24 deployed 7:32 PM by session `sidra-algo-trading-7d`; RS Pullback `run_sweep` running in the `api` container until about 1:15 AM |
| Local shared checkout | on `claude/sip-audit-fixes` (`88861a7`), used by another session for PR #25 |
| This work | branch `claude/research-lab-phase1` from `a4548b0`, in a separate worktree `../sidra-research-lab` so the shared checkout is never switched |
| Working-tree changes | only untracked user documents in the shared checkout (`reports/`, `research_notes/`, a PDF). No unexplained code changes. |
| Development path | the brief names `E:\Intraday_algo`; that path does not exist. The repository is at `D:\Dev Projetcs\Personal Projects\sidra-algo-trading`. |

### Application containers

Code identity was measured by hashing the `app` package's Python source inside each running container.

| Service | Image id | Source hash | Matches checkout `a4548b0` |
|---|---|---|---|
| api | `f6c067188766` | `f9d31bbbca1d43bd` | yes |
| scanner-worker | `e2fc765dffa0` | `f9d31bbbca1d43bd` | yes |
| swing-worker | `9505220ce829` | `0b8319aa7626f503` | **no — older build (about 45 hours)** |
| web | `09c251eed941` | n/a (compiled bundle) | rebuilt with the 7:32 PM deploy; no commit is stamped in, so not provable |

Infrastructure: `postgres:16-alpine` and `redis:7-alpine` (up, healthy); `caddy:2.10-alpine` (LAN TLS on
`0.0.0.0:8443`, from `docker-compose.lan-https.yml` via `COMPOSE_FILE`).

**Documented deploy command does not rebuild all application services.** `CLAUDE.md` says
`docker compose up -d --build api scanner-worker`. That omits `swing-worker` and `web`. This is the cause
of the swing-worker drift.

Other containers on the same Docker host, not part of this project, noted only because they share the
machine: `ha-postgres` (published on `0.0.0.0:5433`), `sidra-trading`, `sidra-db`, `cloudflared-tunnel`.

### Environment variables (names only)

The NAS `.env` defines 96 names. Credential-bearing names: `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`,
`UPSTOX_ACCESS_TOKEN`, `UPSTOX_PIN`, `UPSTOX_TOTP_SECRET`, `UPSTOX_MOBILE_NUMBER`, `UPSTOX_TOKEN_ENCRYPTION_KEY`,
`FIRSTOCK_API_KEY`, `FIRSTOCK_PASSWORD`, `FIRSTOCK_TOTP_SECRET`, `FIRSTOCK_USER_ID`, `FIRSTOCK_VENDOR_CODE`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_CONFIG_ENCRYPTION_KEY`, `JWT_SECRET`,
`POSTGRES_PASSWORD`, `DATABASE_URL`. No value was printed.

Non-secret values read: `APP_ENV=production`, `COOKIE_SECURE=true`, `UNIVERSE_ENABLED=true`,
`MARKET_REGIME_ENABLED=false`, `NSE_CALENDAR_CONFIRMED_YEARS=2026`, `NSE_HOLIDAY_OVERRIDES=` (empty),
`NSE_SPECIAL_SESSIONS=` (empty), `WEB_BIND_HOST=127.0.0.1`, `LAN_HTTPS_BIND=0.0.0.0`, `LAN_HTTPS_PORT=8443`.

## 2. Live-system safety snapshot

| Item | Value | Source |
|---|---|---|
| Application mode | LIVE | `audit_deployment.py` |
| Live trading enabled | true | same |
| Approval mode | AUTOMATIC | same |
| Live broker | UPSTOX | same |
| Live strategy | RS Pullback (`rs-pullback-default`), LIVE. VWAP Pullback, EMA Momentum, CPR Breakout: PAPER. ORB Retest: OFF. SIP Momentum: not in the stored configuration | `check_settings.py` |
| Scanner control state | RUNNING; worker status RUNNING; heartbeat 8:15 PM IST | Redis |
| Emergency stop | not set | Redis `safety:emergency_stop` empty |
| Live activation | armed 8:45 AM, expired 4:45 PM; not revoked | `live_activations` |
| Today's live orders | 3 LIMIT entries at 9:47 AM, all filled; 3 SL-M stops, all `COMPLETE` (all three trades stopped out) | `live_order_submissions` |
| Open live positions | none by the local record (every filled entry matched by a completed stop). Broker book **not queried** | DB only |
| Unresolved orders (`PREPARED`/`UNKNOWN`/`NEEDS_REVIEW`) | 0 | DB |
| Protective stops | present for every entry today | DB |
| Last live reconciliation | 3:20 PM, `CLEAN`, `safe_to_trade=true` | `execution_reconciliations` |
| Daily risk used | ₹750 of ₹750 | `check_settings.py` |

## 3. Test baseline

| Item | Value |
|---|---|
| Backend test functions | 1,947 `def test_` in `apps/api/tests` (136 modules plus `load/`, `recovery/`) |
| Frontend | one Playwright spec, 113 tests, API fully mocked |
| CI | `.github/workflows/ci.yml`, runs on pushes and PRs to `main` only — PRs into `claude/epic-knuth-g8brv1` get **no CI** |
| Python | image `python:3.12-slim`; CI `3.11`; NAS host `python3` is 3.8.15; this machine 3.11.6 |
| Production test path | `scripts/run-tests.sh` on the NAS builds the `api` image and refuses a database that equals the real one. Not run: the brief says not to, and a sweep is running in `api` |
| Local test path used here | a throwaway PostgreSQL 18 cluster in the session scratchpad on `127.0.0.1:55432`, trust auth, database `sidra_test`, migrated with `alembic upgrade head`. No connection to the NAS database exists from this machine |
| Alembic | production at `0030_swing_paper_ledger`, which is head on this branch. No new migration in Phase 1 |
| Frontend build | `npm --workspace @intraday-sentinel/web run build` (Next 15) |

Pre-existing failures found while running related modules (not caused by Phase 1):

* `tests/test_inspect_live_day_labels.py::test_today_is_not_sent_to_the_backfill_script` fails identically
  on the unchanged branch; it depends on the current date and an empty database.
* `ruff check` reports six findings in files Phase 1 does not touch (`extra_strategies.py` SIM300 with
  ruff 0.17; `scripts/selection_study.py`; `scripts/apply_cpr_paper_strategy.py`).

## 4. Database

4.08 GB. Largest: `scanner_evaluations` 1.98 GB, `market_candles` 1.44 GB, `market_indicator_snapshots`
0.65 GB. Candles: 1-minute, 65 instruments, 9 Apr–27 Nov 2026; 3-minute, 29 instruments, ended 16 Oct;
daily, 201 instruments, 19 Nov 2024–26 Nov 2026. The last eight sessions are complete (375 bars) except
17 and 26 Nov (369). `pg_stat_user_tables.n_live_tup` reads 0 for every table (statistics reset or never
gathered; not investigated).

Candidate research data, not yet used: the other session is downloading public Upstox 5-minute data for
NIFTY 200, 2022–2026, into `~/sidra-research/data` on the NAS host.

## 5. Risks carried into Phase 1

1. Sweep promotion writes a LIVE strategy without confirmation, reason or revision.
2. Calendar knows only 2026; trading stops on 1 January 2027 with no warning beforehand.
3. Swing worker on older code; deploy command omits it and `web`; no way to prove what a container runs.
4. Adding a year to `NSE_CALENDAR_CONFIRMED_YEARS` without its holidays would trade every weekday of
   that year, holidays included.

## 6. Phase 1 plan and expected files

| Item | Files |
|---|---|
| Promotion guard | `apps/api/app/api/routes/backtesting.py`; test `tests/test_sweep_promotion_guard.py` |
| Calendar coverage and warning | `apps/api/app/services/trading_calendar.py`, `apps/api/app/services/scheduler.py`, `apps/api/app/api/routes/system.py`; web `features/system/system-health-panel.tsx`, `components/api.ts`, `lib/formatting.ts`; test `tests/test_calendar_coverage.py` |
| Deployment consistency | new `apps/api/app/services/build_info.py`, `apps/api/scripts/verify_deployment.py`, `scripts/verify-deployment.sh`; `infra/docker/api.Dockerfile`, `infra/docker/web.Dockerfile`, `docker-compose.yml`; `CLAUDE.md`; test `tests/test_deployment_consistency.py` |
| Documents | `docs/research-lab/*.md` |

No file overlaps the other session's PR #25 (agreed by message).

## 7. Production actions that need approval (none taken)

* Deploying this branch: merge into `claude/epic-knuth-g8brv1`, then on the NAS
  `SIDRA_BUILD_COMMIT=$(git rev-parse HEAD) docker compose up -d --build api scanner-worker swing-worker web`,
  after `scripts/run-tests.sh`, outside 8:15 AM–3:40 PM, holding `~/sidra-deploy/LOCK`, and not while a
  job runs in `api`. This also brings the stale swing worker up to date.
* Adding the 2027 calendar once NSE publishes its circular (expected mid-December).
