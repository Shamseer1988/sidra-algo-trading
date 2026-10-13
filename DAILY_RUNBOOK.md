# Daily runbook

What you run, when, and what you are looking for. All times IST.

The app trades on its own. Nothing in this file places, modifies or cancels an
order — these are diagnostics. If you run none of them, the day still trades.
You run them to find out *why* it traded the way it did.

Everything runs from the stack directory on the NAS:

    cd /volume1/docker/sidra-algo-trading

---

## What the app does by itself

| Time | Job | What it does |
|---|---|---|
| 08:30 | `upstox_morning_renewal` | Renews the Upstox access token for the day |
| 08:40 | `broker_settlement_catchup` | Fills in charges for earlier days the broker had not settled yet |
| 08:45 | `live_session_open` | Broker login, reconcile, then **arms** the day |
| 09:15 | — | Market opens; the scanner starts taking entries |
| every 1 min, 09:00–15:59 | `live_exit_sweep` | Targets, stop top-ups, trailing, square-off at the cutoff |
| every 2 min, 09:00–15:59 | `live_submission_recovery` | Chases any order whose response we lost |
| every 10 min, 09:00–15:59 | `live_reconciliation_refresh` | Re-reads the broker's book |
| 15:15 | — | Square-off cutoff (before the broker's own) |
| 18:00 | `broker_day_figures` | Pulls the broker's own P&L and charges for today |

If the 08:45 open fails, nothing arms and nothing trades. That is the one
failure worth catching before the open — which is what the 08:15 and 08:35
steps below are for.

---

## 1 — Tonight (once, any time before bed)

Deploy and confirm the running image is the one you think it is.

    cd /volume1/docker/sidra-algo-trading && git pull
    docker compose build --no-cache api && docker compose up -d api
    docker compose exec api python scripts/snapshot.py

**Look for:** the `FEATURES` section with every line present, and a final
verdict of `[OK] Nothing here is blocking`. The `paper tracking` line should
now read `on (never set, which the scanner reads as on)` — not `unknown`.

---

## 2 — 08:15, before the token renewal

Two scripts. Both read-only, both safe at any time.

    docker compose exec api python scripts/snapshot.py
    docker compose exec api python scripts/check_settings.py

**`snapshot.py` — look for:** `application_mode: LIVE`, `live_trading_enabled:
yes`, emergency stop not engaged, Upstox credentials `configured: yes`, and the
instrument master not stale.

**`check_settings.py` — look for:** the numbers you set are the numbers that
will be used, and that they agree with each other. It will tell you if the
daily budget is quietly capping the trade count, or if a per-strategy value is
overriding an account one.

If either prints a blocking verdict, fix it before 08:45. After 08:45 a fix
does not arm the day on its own.

---

## 3 — 08:35, after the renewal and before the open

Rehearse the open. Worth doing tomorrow specifically, because it is the first
session on the new tick-size and R-cap code.

    docker compose exec api python scripts/session_open_dryrun.py

It runs the **real** 08:45 sequence — same calendar check, same runtime guard,
same broker login, same reconcile — and stops immediately before arming.

**Look for:** `Reconcile passed. Stopping before the arm (dry run).` Anything
else is what the 08:45 job will hit too, ten minutes from then.

Two things to know: it writes a reconciliation record and an audit row (the
same ones the real job writes), and if you run it *after* 08:45 it will just
answer `already_armed` and do nothing.

---

## 4 — 08:50, after the open

Confirm the day actually armed.

    docker compose exec api python scripts/snapshot.py

**Look for:** an active live activation with an expiry later today. No
activation at 08:50 means the day will not trade, and the reason will be in
the output.

---

## 4b — 09:20, after the ranking is built

Only while the daily watchlist ranking is on. Two commands, both read-only.

    docker compose logs --since 30m scanner-worker | grep universe_refresh
    docker compose exec api python scripts/universe_applied.py

**The log line — look for:** `scanner.universe_refreshed` with a timestamp near
`09:16`, carrying `candidates`, `eligible` and `selected`. A timestamp of 09:25
means the container is running the old image; no line at all means the refresh
did not run, and the scanner is watching everything.

**`universe_applied.py` — look for:** `[OK] Nothing a strategy scored under
this ranking was outside its selection`, and a `ranking written at` time near
09:16.

Expect a `Not counted above, and why` block under it, and read it rather than
worrying about it. Two things put evaluation rows against an instrument the
ranking refused, and neither is the gate failing: the data-quality gate records
its blocks *before* the universe is consulted, and the 09:15 candle is scanned
before the ranking exists, because a ranking that has not run must not halt
trading. On 13 October five refused names each had one such row — an earlier
version of this script called that a leak, and it was not.

This is the one check no screen could answer. The ranking being correct and the
ranking being *obeyed* are different facts: on 12 October the ranking selected
21 of 27 and all 27 were scanned, because the scanner looked for the ranking at
09:15, before the 09:25 refresh had written it, and kept that empty answer all
day. Every universe screen reported the ranking and looked healthy throughout.
`SCANNED UNDER THIS RANKING BUT NOT SELECTED BY IT` is that failure, named:
a strategy scoring a refused instrument on a candle that opened after the
ranking was written, when the gate demonstrably had it.

Run it again after the close if you want the full day rather than the first few
minutes — it reads the whole session either way, so early in the morning the
`Selected but never scanned` note is expected and means nothing.

---

## 5 — During the session: nothing

Do not run anything on a schedule. Telegram tells you about entries, stops and
targets. If a message looks wrong while the market is open, that is the moment
for:

    docker compose exec api python scripts/inspect_live_day.py

Safe during a live session — the broker side goes through the report client,
which has no method that can place, modify or cancel anything.

---

## 6 — 15:45, after the close

The main one. This is the output to read every day.

    docker compose exec api python scripts/inspect_live_day.py --date 2026-10-08

It answers four questions in one place: what we sent, what the broker said
(including their own refusal wording), what actually filled, and what it risked
— planned against sent, and budget against the real stop.

**Specifically for tomorrow, the two things the recent fixes were about:**

- **Quantity.** Planned vs placed should now land close — roughly 25 of 27,
  not 21 of 27. The entry cap is measured in R now instead of as a percent of
  the share price, so it no longer eats a third of the stop distance on a
  cheap stock.
- **Rejections.** No order should come back refused on price. Every entry and
  stop is rounded to that instrument's own tick from the Upstox master, so a
  ₹0.10-tick share (PAYTM, BHARTIARTL, RELIANCE) will not be sent a ₹x.x5
  price again. If you see `BROKER MESSAGE: ... multiples of the tick size`,
  that fix did not take and I need the output.

---

## 7 — 18:15, after the broker figures job

Run the same script again. It now carries the broker's own charges rather than
our estimates.

    docker compose exec api python scripts/inspect_live_day.py --date 2026-10-08

Then open **Reports → P&L calendar** in the web app and confirm today's cell
matches your Upstox app. Those figures are the broker's, not ours.

If charges are still missing at 18:15, the broker had not settled them yet.
Friday's 08:40 catch-up fills them in — nothing for you to do.

---

## Shortest version

| When | Command |
|---|---|
| Tonight | `git pull` → `docker compose build --no-cache api && docker compose up -d api` → `snapshot.py` |
| 08:15 | `snapshot.py` then `check_settings.py` |
| 08:35 | `session_open_dryrun.py` |
| 08:50 | `snapshot.py` |
| 09:20 | `docker compose logs --since 30m scanner-worker \| grep universe_refresh` then `universe_applied.py` |
| 15:45 | `inspect_live_day.py --date <today>` |
| 18:15 | `inspect_live_day.py --date <today>` + Reports → P&L calendar |

Every command above is prefixed `docker compose exec api python scripts/`.

---

## Other scripts, and when they are the right one

Not part of the daily routine.

| Script | Use it when |
|---|---|
| `audit_deployment.py` | Handing the deployment to someone else, or after changing `.env`. Prints every go-live figure with the source each came from. |
| `verify_upstox_orders.py` | After changing the Upstox app, or if orders start getting rejected at the account level. Tells you the IP you are calling from — the one that has to be registered. |
| `backfill_broker_history.py` | Rebuilding the P&L calendar from the broker's reports after a purge. |
| `purge_history.py` | Clearing local history. Destructive — read it first. |
| `universe_applied.py` | Any day you want to know whether the watchlist ranking actually governed what was scanned, rather than what it ranked. Part of the 09:20 step above while the ranking is on. |
| `why_no_trade.py` | A day that traded less than you expected. Groups every evaluation by the reason it stopped there. |
| `watchlist.py` | After changing `UPSTOX_SUBSCRIPTIONS`. Lists what is actually being watched, with tick sizes and last stored close. |

No script prints a secret. Credentials appear as `configured: yes`, never as a
value, because the usual reason to run one is to paste the output.
