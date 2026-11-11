# sidra-algo-trading

A live intraday trading system for NSE equities. It places **real orders with
real money** through Upstox, automatically, with no human approval step
(`execution_approval_mode: AUTOMATIC`). Treat every change as safety-critical.

## Running things on the NAS

The app runs in Docker on a Synology NAS at `/volume1/docker/sidra-algo-trading`.
It is not on this machine. Reach it over SSH, and **always pass `-T`** — without
it `docker compose exec` allocates a TTY and hangs on a non-interactive SSH:

```
ssh nas "cd /volume1/docker/sidra-algo-trading && docker compose exec -T api python scripts/<script>.py <args>"
```

Services: `api`, `scanner-worker`, `postgres`, `redis`, `caddy`, `web`.
The scanner is `scanner-worker`, not `worker`.

## What may be run, and when

**Read-only. Safe any time, including during a live session.** They write
nothing and cannot place, cancel or modify an order:

```
check_settings.py      daily_review.py        inspect_live_day.py
why_no_trade.py        watchlist.py           candle_coverage.py
strategy_stats.py      trade_patterns.py      entry_edge.py
snapshot.py            audit_deployment.py    universe_applied.py
```

**Heavy. Read-only, but only after 15:30 IST.** `run_sweep.py` loads ~630k
candles (~400 MB) and runs for an hour; `backfill_history.py` hammers the
Upstox API. Either during market hours competes with the scanner for the RAM
and CPU it needs to place orders. Run them with `nohup ... &` and a log file.

**Never run without asking first.** These change live trading control values,
saved settings, or history:

```
apply_settings.py      live-control.sh        apply-*.sh
restore-*.sh           purge_history.py       repair_unbalanced_positions.py
```

Shamseer changes trading settings himself. If one needs to change, say which
one and why, and wait.

**This list is enforced, not just written down.** `.claude/settings.json` has a
PreToolUse hook that refuses any Bash command whose text names one of them. It
matches the text, so it also refuses a command that merely mentions one -- that
is the safe direction and costs only the occasional `grep`. It is a guardrail
against a mistake, not a security boundary: anything that assembles the name
from pieces goes straight past it.

The twelve read-only scripts are allow-listed by their full command, so they
run without a prompt. Anything else still asks.

## Deploying a code change

```
ssh nas "cd /volume1/docker/sidra-algo-trading && git pull origin <branch> && scripts/run-tests.sh"
# only once the suite is green:
ssh nas "cd /volume1/docker/sidra-algo-trading && docker compose up -d --build api scanner-worker"
```

**`docker compose exec` runs in the container that is already up, not in the
image you just built.** `scripts/run-tests.sh` builds the image and tests it in
a throwaway container, so a green suite says nothing about what the running API
is executing. Until `up -d --build` recreates them, `exec` is still the old
code. This has bitten twice: a suite reported 2217 passing against an image
that predated two of the commits under test, and a new report section silently
did not print.

Settings live in Postgres, so a setting changes without a deploy; code does not.

## Testing

`scripts/run-tests.sh` — never `pytest` against the trading database. The suite
writes to the same tables the live account's history lives in; pointed at
production it would pass and take the record of every trade with it. The script
resolves its own `_test` database and refuses if that equals the real one.

Load tests are off the default path (`scripts/run-tests.sh tests/load/ -q` runs
them). They measure the NAS, not the code, and answered differently to the same
commit.

## The rules that are not negotiable

- **Order placement, the live risk gates and protective-stop logic** are not
  touched without asking first, however small the change looks.
- **One change per decision**, not per day. Two settings moved in one session
  means neither can be attributed, and `daily_review` says so in section D.
- **No new Alembic migration** unless there is no alternative; ask first.
- **Never print a secret.** Diagnostics report credentials as "configured: yes".
  A `printenv DATABASE_URL` once put the Postgres password into a chat window.
- A stop order does **not** guarantee the loss is bounded: Upstox converts SL-M
  into SL with its own 1-2% limit band, so a gap through it may not fill.
- Do not claim a strategy is profitable without out-of-sample and forward
  results. Forty trades is an anecdote.

## Context worth having

- Capital ₹25,000, risk 0.5% (₹125) a trade, 3 trades a day, 1:2, window
  09:30-11:30, flat by 15:00.
- Four strategies exist; only **RS Pullback** is enabled. ORB Retest and VWAP
  Pullback were disabled on sweep evidence, EMA Momentum never traded.
- Four daily ceilings, all counted from fills: account trades, per share, per
  sector (`app/services/sectors.py`), per strategy. The per-sector map is keyed
  by trading symbol and `watchlist.py` warns when the watchlist outgrows it.
- Explanations to Shamseer are written in **Malayalam**.
