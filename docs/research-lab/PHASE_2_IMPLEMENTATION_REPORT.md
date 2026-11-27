# Phase 2 — Backtesting correctness

Branch `claude/research-lab-phase2`, built on Phase 1 (`ae17c59`, PR #26) merged with the other session's
PR #25 (`88861a7`, SIP audit fixes), because #25 already changes `services/backtesting.py`: it adds a
strategy time exit, the replay's time-of-day volume baseline and the paper engine's SL-limit band. Phase 2
builds on those rather than beside them. **Not deployed. Merge order: #25, then #26, then this.**

No change to order placement, live risk gates, protective stops, exits, reconciliation or the live
scanner. No migration. Results stay in the existing `backtest_*` tables and JSON.

## What was wrong, and what changed

| # | Problem (audit, 27 Nov) | Change | Where |
|---|---|---|---|
| 1 | Each strategy was replayed on each share independently, taking every signal. Live takes the first ones until a ceiling closes the day | **Portfolio-day simulator.** The replay emits every signal (`all_signals=True`), resetting the strategy state the way a live refusal does, so a refused setup can form again later that session as it does live. Each session is then walked in signal order and every candidate is kept or refused by the rules live applies, in live's order: price band, day halted, account ceiling, per share, per sector, per strategy, per side, cooldown, daily risk budget (this trade included), open positions, open exposure (with leverage). After one signal of a chain (strategy × share × day) is taken, its later signals are dropped, as live never produces them | new `app/services/portfolio_simulation.py`; `backtesting.run_completed_candle_backtest(all_signals=…)` |
| 2 | `max_trades_per_day` and `cooldown_minutes` were sweepable but the replay never read them | Honoured by the portfolio pass. Each sweep variant is simulated as its own account | `backtest_sweep._one_pass`, `run_parameter_sweep` |
| 3 | No 3:00 PM square-off unless a strategy set its own | The replay caps each strategy's square-off at the account's `session_square_off_time` with `exit_rules.under_account_deadline`, the function `live_exit_manager` uses. Due at a candle's close, filled at the next open (PR #25's rule) | `backtesting.run_completed_candle_backtest` |
| 4 | A gap through the stop was booked at the stop, a price that never traded | The stop fill now uses the paper engine's own `PaperOrderManager._stop_fill`: at the stop when traded through; at the open when the candle opens beyond it; beyond Upstox's SL-limit band (2%) only when price returns to the band's edge, otherwise the order rests | `backtesting._exit_trade` |
| 5 | The daily loss stop was not simulated | The portfolio pass halts a session once realised net P&L reaches the loss limit or profit target, through `daily_limits.evaluate` | `portfolio_simulation` |
| 6 | API sweeps used the environment's indicator periods; `run_sweep.py` too | Both use `indicator_settings.effective`, as the single backtest and the scanner do. `rvol_baseline_sessions` is now carried into a sweep | `routes/backtesting.create_sweep`, `scripts/run_sweep.py`, `backtest_sweep._settings_namespace` |
| 7 | The recommended combination was the best **validation** row, which makes validation part of the tuning | `_rank` chooses on in-sample only; validation is reported, never used to choose. The script's table is ordered in-sample and says how many variants were tried | `backtest_sweep._rank`, `scripts/run_sweep.py` |
| 8 | No count of trials, no interval, no cost sensitivity | `SweepResult.variants_tested`; every metric block gains `average_r_ci95` (seeded bootstrap, ≥ 10 trades) and `average_r_at_double_costs` | `backtest_sweep._metrics` |
| 9 | No walk-forward | `walk_forward(trades_by_index, dates, train_sessions, test_sessions)`: choose on each training window by the in-sample rule, score the next window, stitch the out-of-sample trades. `run_sweep.py --walk-forward 40,20` prints it | `backtest_sweep.walk_forward` |
| 10 | Per-sector ceiling absent from research | The API and script resolve each share's name and sector once (`sector_lookup`), with the scanner's fallback when a name cannot be read | `routes/backtesting.sector_lookup` |

The single backtest on the Backtesting screen now runs through the portfolio simulator too. Its stored
summary carries a `portfolio` block: candidates, taken, refused, superseded, refusals by reason, halted
days.

## Where the simulator is an approximation

Stated in the module docstring as well:

* **Loss stop on realised P&L.** Live reads the broker's P&L, which includes open positions' marks. The
  simulator sees completed trades, so it halts on closed losses and can halt later than live when an open
  trade is deep in loss.
* **Cooldown counts taken signals.** Live counts accepted evaluations, which also include signals a later
  risk gate (budget, positions, exposure) refused. They differ only in that case.
* **Same-minute ties** are broken by strategy id, then share. Live breaks them by whichever candle
  callback reaches the entry lock first, which cannot be replayed.
* **The entry candle is not checked for the stop.** Exit checks start on the candle after the entry
  candle, as before. A stop hit inside the entry candle is missed. Unchanged from the existing engine;
  listed for Phase 3.
* Two money formulas (daily risk budget, exposure ceiling) are restated rather than imported from
  `live_risk`, so research code does not import the live risk engine. A test holds them equal.

## Tests

New: `tests/test_portfolio_simulation.py` (15), `tests/test_backtest_correctness.py` (17). Changed:

* `tests/test_backtest_sweep.py::test_run_parameter_sweep_prefers_a_validated_combination` is replaced by
  `…_chooses_on_the_in_sample_half_only`. **Deliberate:** the old test asserted the holdout-ranking rule
  this phase removes. The new one asserts the opposite and says why.
* `tests/test_backtest_does_not_block.py`: the pinned name of the threaded call is now
  `run_portfolio_backtest` (it wraps `run_completed_candle_backtest`).

Checks that the tests test something: the two gap-fill tests fail against the PR #25 exit code; the
sweep-honours tests fail against it at import (the old replay had no `all_signals`).

### Full backend suite, this machine (Windows, Python 3.11, throwaway PostgreSQL 18, no Redis)

| Branch | Result |
|---|---|
| Phase 2 (this branch) | 2,761 passed, 15 failed |
| PR #25 unchanged (`88861a7`) | 2,684 passed, 15 failed |
| Failures only on Phase 2 | **none** — the 15 are identical on both |

The 15 pre-existing failures are environmental, not caused by this work, and **must be re-run on Linux
before any deploy** (`scripts/run-tests.sh` on the NAS), because several guard live-path safety
properties:

* 11 source-scanning tests that build module paths and fail on Windows path separators
  (`test_model_attribute_reads` ×5, `test_live_entry_bridge` ×4, `test_live_protection` ×2,
  `test_broker_field_round_trip`, `test_notification_settings`);
* `test_backtest_does_not_block::…keeps_the_api_answering` (Windows timer resolution; uses a stand-in
  workload, not the engine);
* `tests/load/test_system_load.py::test_50_concurrent_authenticated_users_load` (no Redis here; load
  tests are off the default path on the NAS).

Ruff check and format pass on every changed file.

## Not done in Phase 2 (by design)

* No change to how results are stored (no migration). Walk-forward output is printed by `run_sweep.py`
  and returned by the service; it is not yet persisted. That waits for the research schema (Phase 4).
* No UI change. The Backtesting screen still shows the old columns; the new `portfolio` summary block
  and metric keys are in the stored JSON for Phase 5.
* No change to where research runs. Sweeps still execute in the API process (Phase 3 moves them).
* No new strategy, and no strategy setting changed.

## Phase 3 plan (research-worker isolation), for approval

1. A `research-worker` Compose service from the API image, own `env_file` without broker or Telegram
   secrets, `cpus` and `mem_limit`, no published port.
2. A job table. A migration is unavoidable here; ask first. Columns: type, payload, status, attempts,
   heartbeat, timeout, cancel flag, result reference. The API's backtest and sweep routes enqueue and
   return a job id; the worker claims with `FOR UPDATE SKIP LOCKED`.
3. The worker refuses heavy jobs between 9:00 AM and 3:40 PM on trading days.
4. A test that research modules import nothing from `live_*`, `upstox_orders`, `broker_adapter`,
   `firstock.orders`.
5. Fix the entry-candle stop gap noted above, with a test.
6. Then the first real job: RS Pullback, 1-minute data, 2022–2026, walk-forward. This needs the history
   from Phase 4, or the other session's public-API 1-minute download.
