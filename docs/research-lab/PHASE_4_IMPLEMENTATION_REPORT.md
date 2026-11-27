# Phase 4 — Historical data and research storage

Branch `claude/research-lab-phase4`, on top of Phase 3 (PR #28). **Not deployed.** Contains migration
`0032_research_data` (new `research` schema) and `infra/sql/research_role.sql`. Neither has been applied
to production.

## 1. What the provider actually supplies (measured 27 Nov 2026, not assumed)

Upstox's public v3 historical-candle endpoint. No credentials are sent; the research worker holds none.

| Question | Answer | How measured |
|---|---|---|
| Earliest 1-minute bars | **January 2022.** June 2021 and December 2021 return `success` with zero candles; January 2019 too | RELIANCE, one request per month |
| A full month | Complete: January 2022 = 7,500 bars = 20 sessions × 375 | RELIANCE |
| The current session | Not included (end-of-day history) | 27 Nov returned 0 bars during the evening |
| Recent sessions | Complete: 20–26 Nov = 1,500 bars = 4 sessions (24 Nov a holiday) | RELIANCE |
| The index | NIFTY 50 (`NSE_INDEX|Nifty 50`) available, 375 bars a session | smoke run |
| Rate limits | No rate-limit headers returned. The client stays at 2 requests/s and backs off on 429 and 5xx | response headers |
| Price adjustment | **Adjusted by the provider for corporate actions.** RELIANCE ≈ ₹1,135 on 31 Jan 2022 in this data; it traded ≈ ₹2,400 that day, before the Oct 2024 1:1 bonus | price check |
| Volume adjustment | **Not verified** | — |
| "No data" vs "not available" | Indistinguishable (both `success`, zero candles), so zero bars is recorded as missing, never as a holiday | — |

Scale: NIFTY 200 at 1 minute from January 2022 is about 200 × 1,215 sessions × 375 ≈ 91 million bars,
about 12,000 requests (≈ 1.7 hours at 2/s). The NAS has 7.9 TB free. The other session's 5-minute NIFTY 200
download of the same span takes 215 MB.

## 2. A finding about SIDRA's own live candles (not fixed; needs your decision)

The provider's recent bars were compared with the candles SIDRA built from the live feed, read-only from
production, for RELIANCE:

| Session | Live-recorded volume | Provider volume | Ratio |
|---|---|---|---|
| 15 May 2026 | 19,974,218 | 19,974,218 | identical (those bars came from the backfill) |
| 15 Oct 2026 | 515,239,565 | 23,293,551 | 22× |
| 20 Nov 2026 | 765,388,204 | 13,166,219 | 58× |
| 25 Nov 2026 | 1,322,177,275 | 18,776,265 | 70× |
| 26 Nov 2026 | 2,052,940,673 | 20,022,652 | 103× |

On 26 Nov:
* 145 of 369 minutes carried more than 10 times the provider's volume, spread through the day.
* Live high–low ranges were narrower than the provider's in 238 of 369 minutes (live ticks miss
  extremes).
* 6 minutes were missing (15:15–15:19 and 15:29).
* Closes were identical in 200 of 369 minutes and full OHLC in 56; the largest close difference was 0.32%.

**What it means.**
* Live volume is inflated 20 to 100 times, which distorts **VWAP, session RVOL and the time-of-day RVOL
  baseline**. That affects VWAP Pullback, EMA Momentum and SIP Momentum, all paper. **RS Pullback's live
  rules use EMA, ATR and relative strength, not volume**, so it is probably unaffected; not verified.
* A backtest on live-recorded candles sees narrower bars than really traded, so it under-detects stop
  hits and is optimistic.
* The likely cause is the volume delta in `candle_aggregation` from the feed's cumulative traded volume.
  This is **not verified** and **not changed**: it is the live path.

Recommendation: fix the volume aggregation (with your approval). Until then, use provider datasets, not
`market_candles`, for any research that involves volume or intrabar ranges.

## 3. What was built

| Piece | File | What it does |
|---|---|---|
| Research schema | `alembic/versions/0032_research_data.py`, `db/models.py` | `research.instrument`, `research.universe_membership`, `research.dataset`, `research.dataset_file`, `research.dataset_coverage`. Tested up, down and up locally |
| Provider client | `services/research_data.UpstoxPublicHistory` | Public endpoint, no credentials, `|` URL-encoded, 2 req/s, retries only on 429, 5xx and transport errors |
| Storage | `research_data.encode_bars`, `backfill` | One gzip CSV per instrument and month on the `research_data` volume. Deterministic bytes, so the same bars give the same SHA-256. Bars kept exactly as sent, duplicates included. Atomic writes; commit per file |
| Resumable backfill | `research_data.backfill` | Benchmark first. A finished month already on disk with its recorded hash is never fetched again; a damaged file is fetched again; the current month is always refreshed. Stops between files when the research window closes |
| Coverage | `research_data.compute_coverage` | Per instrument and session: bars, expected bars (the **benchmark's** count that day, so special sessions need no holiday list), missing, duplicates, bad OHLC, zero volume, outside 09:15–15:30 |
| Versioning | `research_data.finalize` | Re-hashes every file, fixes a dataset content hash, marks it COMPLETE; a complete dataset cannot be reopened; a file changed afterwards is detected on read |
| Reading | `research_data.read_dataset` | Dataset bars as candles for the backtester, duplicates dropped |
| **Calendar for replay** | `research_data.DatasetCalendar`, `replay_calendar` | See section 4 |
| Runs on datasets | `research_runs.candles_for` | A backtest or sweep may name `dataset_id`. Only a COMPLETE dataset with a matching timeframe is accepted. The run's summary records the dataset id, name, content hash and price adjustment |
| Backfill job | `research_runs.run_backfill_job` | New job type `backfill` for the research worker |
| Window pause | `research_worker.supervise(window_open=…)` | Any job still running when the research window opens at 9:00 AM is stopped and re-queued without spending an attempt; a backfill resumes from its last file |
| Scripts | `scripts/research_import_universe.py`, `research_backfill.py`, `research_coverage.py` | Import a universe list into `research.*`; queue a backfill job; a read-only coverage report |
| Database role | `infra/sql/research_role.sql` | `sidra_research`: read market data and settings; write research tables, the job queue and backtest results; nothing else. **Tested on a real Postgres: refused on `live_order_submissions` and on writing `application_settings`** |
| Compose | `docker-compose.yml`, `api.Dockerfile` | `research_data` volume and `RESEARCH_DATA_DIR`; `edge` network for outbound HTTPS to the public endpoint (still no ports, no credentials); **`mem_limit` lowered to 1 GB** after measuring the NAS (5.8 GB total, about 1.6 GB available) |

## 4. A trap found and closed: pre-2026 history produced no trades at all

The first real-data run, RS Pullback on two shares in January 2022, produced **zero candidate signals**.
Every in-window candle was refused as "Trend indicators are not ready".

The cause: `market_calculations.session_candles` keeps only candles the application calendar calls open.
That calendar confirms only 2026, so every 2022 candle was dropped, and no EMA, ATR or relative strength
was ever computed. Nothing failed; the backtest simply never traded. **Any backtest on pre-2026 data
would have reported "no trades" as if it were a result.**

The fix is research-only. Inside the research worker's job process, a dataset run installs a calendar
whose sessions are the days the dataset's benchmark traded, and restores the previous one afterwards. The
scanner's calendar and the live trading code are not touched. A regression test shows a 2022 replay gets
no EMA without it and does with it.

After the fix, the same run gave 738 candidates, of which 23 were taken. The rest were refused by the
loss stop (217) and the open-position limit (57), or superseded within a chain (441).
**These numbers are a pipeline check, not a result.** The run used the code's default controls, not
production's ₹250 risk, and covered one month and two shares.

## 5. Survivorship and other limits, stated

* **Universe history is not available.** The import records today's NIFTY 200 and marks every row
  "current constituents only; historical membership unknown (survivorship bias)". A dated constituent
  history (NSE index-change circulars) is not fabricated here.
* Price adjustment is the provider's. Volume adjustment is not verified.
* The research worker still uses the application's database role until `research_role.sql` is applied
  and the worker's own `DATABASE_URL` is set.
* The entry-candle stop gap from Phase 2 remains open. It goes with equivalence tests against recorded
  live days, which section 2 shows have to use provider data, not live candles.

## 6. Tests

`tests/test_research_data.py` (19). It covers:
* client: no credentials, key encoding, retry only on 429, failure on 4xx and on a refused status;
* month ranges; deterministic files; day checks;
* backfill: benchmark first, resume, refetch of a damaged file, refresh of the current month, stop when
  the window closes;
* coverage against the benchmark, including a short special session and an absent day;
* finalise, immutability and tamper detection; reading the dataset;
* runs refusing an unfinished or mismatched dataset and naming a finished one;
* the backfill job end to end; the Compose volume; the window pause refunding the attempt;
* the universe import; the coverage summary; the calendar trap.

All research tests pass (76 across Phases 2–4).

Full backend suite on this machine (Windows, throwaway PostgreSQL 18, no Redis): Phase 4 has 2,814 passed
and 16 failed; Phase 3 has 2,795 passed and 16 failed. The failure sets are **identical**: the same
Windows-environment failures as before. `scripts/run-tests.sh` on the NAS is still required before
deploying.

Live checks run locally against the throwaway database: three real requests built and finalised a
dataset, then a portfolio backtest ran on it.

## 7. Production actions needed (all need approval; none taken)

1. Merge #25 → #26 → #27 → #28 → this, then run `scripts/run-tests.sh` on the NAS.
2. Run `alembic upgrade head` (0031 and 0032; both add only).
3. Deploy with `research-worker`, then run `scripts/verify-deployment.sh`.
4. Optionally run `infra/sql/research_role.sql` and give the worker its own `DATABASE_URL`.
5. Import the universe, then queue the first backfill outside market hours, for example:
   `research_import_universe.py --file … --universe NIFTY200 --as-of 2026-11-27`, then
   `research_backfill.py --dataset nifty200-1m-2022 --timeframe 1minute --start 2022-01-01 --end 2026-11-26 --universe NIFTY200`.
6. **Separately:** a decision on fixing live candle volume (section 2).

## 8. Phase 5 plan (Research Lab infrastructure), for approval

1. Research entities: hypothesis, experiment (pre-registered metric, sealed holdout range, variant
   count), experiment run, walk-forward fold, validation result, report. Each run records the dataset
   hash, the strategy code fingerprint (`build_info`), parameters and assumptions.
2. Screens inside SIDRA: Research dashboard, Datasets (coverage), Jobs, Experiments, Results
   (walk-forward, bootstrap intervals, cost sensitivity). Read-mostly, with no promote button; a
   recommendation links to the Builder.
3. Statistics: deflated Sharpe or a trial-count adjustment, regime split.
4. The entry-candle stop gap, with equivalence tests on provider data for recorded live days.
5. Then Phase 6: RS Pullback, 2022–2026, walk-forward, as the first pre-registered experiment.
