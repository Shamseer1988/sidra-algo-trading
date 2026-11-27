# Phase 6, step 1 -- research in bounded memory (28 Nov 2026)

## Why

The first NIFTY 200 backfill (job `4cf2ddfd`) was cancelled after four shares.
The research worker hit its 1 GB limit while it shared the container with a
45-session RS sweep. Measured on minute data, a loaded candle is about 712
bytes. So the planned experiment, NIFTY 200 over 3.5 years at 1 minute (about
65 million candles), would need about 46 GB. Selection, holdout, coverage and
dataset backtests all loaded everything at once, so none of them could ever
have run.

## What changed (research code only)

| Piece | Before | Now |
| --- | --- | --- |
| Experiment selection and holdout | `read_dataset` loaded the whole period, then ran one replay | `research_replay.replay_taken`: one month at a time, one share at a time. The portfolio pass runs per month and keeps only the taken trades. |
| Backtester | replayed every session it was given | New `evaluate_from` option (default off): earlier sessions are history only. |
| Coverage | every bar of every share, plus one ORM object per row | one share at a time, bulk insert |
| Benchmark sessions and regimes | from the loaded benchmark series | `benchmark_days`, read a month at a time |
| Dataset backtest or sweep jobs | loaded anything | refused before reading if over 500,000 candles (`DatasetTooLarge`) |
| Replay calendar | asked about the same minute ~190 times a session | remembers each instant's answer (bounded). This was 70% of replay time, profiled. |

Correctness is pinned by `tests/test_research_replay.py`:
- The month-at-a-time replay takes exactly the trades a single full pass
  takes, across a month boundary, for three shares and two variants.
- `evaluate_from` leaves later trades identical.
- Nothing past the end date is read.

Nothing on the live path changed. `market_calculations`, the scanner, order
placement, risk gates and protective stops are untouched.

## Measured

- **Backfill plus coverage:** peak about 90 MB, flat from 4 to 12 shares (it
  was never the backfill's own memory; the sweep held most of the container).
- **Replay speed, this PC:** 1.6 s per share-session at 375 minutes (was 2.9 s
  on the dataset calendar, 2.4 s on the app calendar). Extra variants in a
  shared pass are almost free (1 → 6 variants: same time).
- **Memory of a replay:** one share-month plus its warm-up (about 73 days),
  whatever the period or the universe.

## What still limits Phase 6: CPU

The remaining cost is `market_calculations.indicator_snapshot`. For every new
candle it recomputes the session's indicators over the whole session so far,
which is quadratic in the minutes of a session. That module is shared with the
live scanner, so it is not changed here without approval.

At 1.6 s per share-session (PC; the NAS is not yet measured):

| Universe | Selection Jan 2022-Jun 2025 (~860 sessions) |
| --- | --- |
| NIFTY 200 (201 shares) | ~77 h |
| NIFTY 50 | ~19 h |
| Live watchlist (34) | ~13 h |

The research worker runs only outside 9:00 AM-3:40 PM IST on trading days and
on one CPU. NIFTY 200 would therefore take one to two weeks of nights and
weekends, and longer if the NAS is slower than this PC.
