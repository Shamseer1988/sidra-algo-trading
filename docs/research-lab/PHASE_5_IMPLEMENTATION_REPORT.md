# Phase 5 — Research Lab infrastructure

Branch `claude/research-lab-phase5`, on top of Phase 4 (PR #30). **Not committed, not deployed.**
Contains migration `0033_research_experiments` (three tables in the `research` schema), not applied to
production.

## What was built

### Pre-registered experiments (`services/research_experiments.py`, migration 0033)

The lifecycle, and what each step cannot do:

| Status | How it gets there | What is fixed |
|---|---|---|
| DRAFT | `POST /research/experiments` | Hypothesis, dataset (must be COMPLETE, 1-minute), instruments, base strategy, parameter grid (and so the variant count, ≤ 60), selection period, sealed holdout after it, walk-forward window sizes, and the mean-R threshold. The strategy, trading controls and charges are copied as they stand at creation |
| QUEUED | `…/register` | Everything above is frozen (`registered_at`). A selection job is queued |
| SELECTED | the `experiment_selection` job | The grid is searched on the selection period. **The holdout's bars are never loaded**: the dataset is read only up to `selection_end`, with 70 days of warm-up whose trades are discarded. Walk-forward folds re-choose on each training window and score the next. The final parameters are the best in-sample over the whole selection period |
| HOLDOUT_SCORED | `…/holdout` with `confirm_single_use`, then the `experiment_holdout` job | The chosen parameters only, run once on the holdout. `holdout_used` is set with the result, and a second scoring is refused. PASSED requires mean R ≥ the registered threshold **and** a 95% interval above zero |
| FAILED_TO_RUN | a crashed selection | Not left stuck in QUEUED |

A crashed holdout job returns the experiment to SELECTED, because no holdout number was produced or seen,
so the holdout is still unused.

Each result row records the code fingerprint (`build_info`) and the dataset's content hash.

### Statistics (`services/research_statistics.py`)

* Count, mean R, standard deviation, t-statistic, win rate, and a seeded bootstrap 95% interval.
* **Deflated Sharpe ratio** (Bailey and López de Prado, 2014). It gives the probability that the per-trade
  Sharpe ratio is real after choosing the best of N variants, allowing for skew and fat tails. The
  variance of Sharpe ratios across the variants actually tried is used when available. Below 0.95 it
  cannot be told apart from selection luck.
* Cost sensitivity: mean R at 1×, 1.5× and 2× charges.
* Regime split: the same summary on benchmark-up and benchmark-down days.

### The entry-candle stop gap (`backtesting._exit_trade`)

The replay filled the entry at the entry candle's open but only started checking the stop on the
following candle. Live places the protective stop seconds after the fill, so a stop touched later in
that minute is real. It now counts. **This changes backtest results** (they get more conservative). Only
the stop is checked on the entry candle. Whether the target was touched before or after the stop inside
one bar cannot be known, and the target is never assumed.

### Screens (`features/research/research-lab.tsx`, menu "Research › Research Lab")

* **Overview:** worker status and research window, dataset and experiment counts, holdout passes, and how
  an experiment decides.
* **Datasets:** status, period, instruments, bars, sessions, % of instrument-sessions complete, price
  adjustment, content hash.
* **Experiments:**
  * new hypothesis and draft experiment (ADMIN);
  * the pre-registration as recorded, with "Register and run selection";
  * the selection result: stitched out-of-sample statistics, a regime split, the deflated Sharpe in
    words, cost sensitivity and the folds;
  * "Score the holdout", which asks in place, states that it is once only, and sends
    `confirm_single_use`;
  * the holdout result, its verdict and the rule.
* **Jobs:** every research job, its outcome, and Cancel (ADMIN).

There is no promote button anywhere on this screen. A browser test checks that.

## Tests

* `tests/test_research_experiments.py` (16) covers:
  * the statistics: deflated Sharpe falling as trials rise, noise below 0.5, cost sensitivity, regimes;
  * the entry-candle stop;
  * five ways an unfair experiment is refused;
  * registering once;
  * **selection never requesting a bar past `selection_end`**;
  * the holdout scored once and refused a second time;
  * a crashed holdout not spending the holdout, and a crashed selection being marked;
  * parameter routing;
  * the holdout route requiring confirmation.
* Browser: `Research Lab` in `tests/e2e/terminal.spec.ts`. It covers the tabs, the numbers rendered, no
  promote button, and the holdout sending a request only after the in-page confirmation. **The full
  Playwright suite passes: 115 tests.**
* Web: `tsc`, `eslint` and `next build` pass.
* Migration 0033 tested up, down and up locally.
* One safety test caught a real mistake in this phase: `test_model_attribute_reads` refused
  `row.__table__` on an annotated model. The code was changed, not the test.

Full backend suite, this machine, Python's UTF-8 mode (`PYTHONUTF8=1`), throwaway PostgreSQL, no Redis:
**2,847 passed, 5 failed.** All five fail identically on the unchanged `5dcbcf2`:

* `test_live_entry_bridge::…offers_a_live_entry…` and
  `test_live_protection::…exit_through_the_entry_gates` look files up by forward-slash path keys
  (`KeyError: 'services/scanner_orchestration.py'`), which a Windows checkout does not produce;
* `test_entry_race::…both_entries_go_through` and `test_backtest_does_not_block::…keeps_the_api_answering`
  depend on timing;
* `tests/load/test_system_load.py` needs Redis.

Before the fix to `research.py`, the UTF-8 run also showed `test_model_attribute_reads[api/routes/research.py]`.
That failure was real and is fixed.

**`scripts/run-tests.sh` on the NAS (Linux) is still required before any deploy**, because two of the
five guard the live path and can only be checked there.

## A correction to earlier reports

The Phase 2–4 reports attributed most of the 15–16 local failures to Windows path separators. Running in
Python's UTF-8 mode (`PYTHONUTF8=1`) shows most are a **text-encoding** problem on this machine: the
source-scanning tests read files with the Windows default code page. They are still environmental, and
they pass on Linux. The UTF-8 run below gives the real local count.

## Production actions needed (all need approval; none taken)

1. Merge in order #26 → #27 → #28 → #30 → this. Run `scripts/run-tests.sh` on the NAS.
2. Run `alembic upgrade head` (0031, 0032, 0033; all add only).
3. Deploy with `research-worker`, then run `scripts/verify-deployment.sh`.
4. Backfill a dataset after 3:40 PM, then register the first experiment.

## Phase 6 plan (strategy discovery), for approval

The first experiment, registered before any result is seen:

* **Hypothesis:** RS Pullback, the strategy trading real money, has a positive expectancy after charges
  on NIFTY 200 1-minute data.
* **Dataset:** `nifty200-1m-2022` (January 2022 to 26 November 2026).
* **Selection:** 3 Jan 2022 – 30 Jun 2025. **Holdout:** 1 Jul 2025 – 26 Nov 2026, sealed.
* **Grid:** small, about 6–12 variants, around the live settings (for example `minimum_score` 65/71/77 ×
  `rs_threshold_percent` 0.2/0.3/0.4). Not a search for something that works.
* **Windows:** train 60 sessions, test 20.
* **Success:** mean R ≥ 0.10 with the interval above zero on the holdout, and a deflated Sharpe ≥ 0.95 on
  the walk-forward.
* **Known biases, stated:** survivorship (today's NIFTY 200), provider-adjusted prices.

If it fails, the honest outcome is that the live strategy has no demonstrated edge, and that decision
belongs to you. Other candidates (SIP, VWAP, ORB, sector-relative momentum) follow the same process, one
experiment each.
