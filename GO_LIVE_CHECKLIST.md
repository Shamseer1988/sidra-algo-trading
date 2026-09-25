# Go-live checklist — existing NAS deployment

A do-it-in-order checklist for taking the NAS from the current (pre-Upstox-live)
build to placing real orders. Tick as you go; it is designed to be stopped and
resumed.

The *why* behind each step is in **`GOING_LIVE_WITH_UPSTOX.md`**. This file is
the doing.

**Run Phases 1–5 after 15:30 IST or at a weekend.** Do not rebuild containers
while a session is running.

---

## Before you start

- [ ] You have a static IP from your ISP
- [ ] You can sign in to <https://account.upstox.com/developer/apps>
- [ ] You can reach the NAS shell
- [ ] You have 60–90 minutes

**Set this once and paste it into every shell you open:**

```sh
export SIDRA=/volume1/docker/sidra-algo-trading   # your real path
cd "$SIDRA"
```

---

## Progress

| Phase | What | Can do today |
|---|---|---|
| 1 | Deploy the current build | yes |
| 2 | Static IP + Upstox app | yes |
| 3 | `.env` changes | yes |
| 4 | In-app configuration | yes |
| 5 | Test a real order (lock still on) | **yes** |
| 6 | I remove the lock | needs Phase 5 output |
| 7 | First supervised live session | after Phase 6 |

---

## Record your answers here

Fill these in as you go. Phase 6 needs them.

```
Starting commit      : ____________________
Starting migration   : ____________________
Outbound IP          : ____________________
IP registered at     : ____ / ____ / 2026
Upstox app type      : Algo Trading App?  yes / no
Dry run stage 2      : confirmed / refused / unknown
```

---

# Phase 1 — Deploy

### 1.1 Where are you now?

```sh
git log --oneline -1
docker compose exec api alembic current
```

→ Write both into the box above.

### 1.2 Back up — do not skip

Eight migrations are about to run.

```sh
./scripts/backup-postgres.sh
ls -lh backups/
```

- [ ] A real file appeared, not an empty one

> **A backup you have not seen succeed is not a backup.** If this script has
> never run on this NAS, read its output rather than assuming.

### 1.3 Pull and rebuild

```sh
git fetch origin main
git checkout main
git pull origin main

docker compose build api scanner-worker web
docker compose up -d
```

### 1.4 Migrate

The image does **not** run migrations on start.

```sh
docker compose exec api alembic upgrade head
docker compose exec api alembic current
```

- [ ] It reports **`0026_broker_day_snapshots`**

> 🛑 **STOP** if it reports anything else. Send me the output.

### 1.5 Health

```sh
docker compose ps
docker compose logs --tail=50 api
```

- [ ] `postgres`, `redis`, `api`, `scanner-worker`, `web` all healthy
- [ ] No errors in the API log

### 1.6 Open the terminal

- [ ] It loads and you can sign in

> **The menu changed.** Seven sections now — Dashboard, Strategies, Scanner &
> Universe, Orders & Positions, History, Risk, Settings — with everything
> diagnostic under a collapsed **Admin & diagnostics**.
>
> "Live Gates" → **Admin & diagnostics → Live readiness**
> "Risk Center" → **Risk**
> "Journal" → **History → Signal journal**

---

# Phase 2 — Static IP and the Upstox app

> **The static IP is not a setting in `.env`.** You register it *at Upstox*.
> The only `.env` line is the attestation in Phase 3.

### 2.1 Find the address orders will actually leave from

Run it **from inside the API container** — that container is what talks to
Upstox, so its view is the one that counts.

```sh
docker compose exec api python -c "import httpx; print(httpx.get('https://api.ipify.org').text)"
```

- [ ] Written into the box above

This is **not** your NAS's LAN address, not your router's WAN page, and not what
a website tells you from your phone.

### 2.2 Rule out what silently changes it

Relevant to you — two of these are already in your stack:

| | Effect on outbound orders |
|---|---|
| Cloudflare Tunnel | **Safe.** Inbound only. |
| Tailscale, plain | **Safe.** Peer-to-peer only. |
| **Tailscale exit node enabled** | **Breaks it** — everything outbound leaves via the exit node |
| Any VPN on the NAS | **Breaks it** |
| CGNAT from the ISP | **Breaks it** — shared and changeable |

- [ ] 2.1 returned my known static IP

> 🛑 **STOP** if it did not. Something in that table is in the path. Find it
> before registering anything.

### 2.3 Confirm the app may place orders

**This is the single thing most likely to stop you, and it is invisible until an
order is rejected.** A market-data app streams quotes perfectly and refuses
placements.

1. <https://account.upstox.com/developer/apps>
2. Open the app whose key is `UPSTOX_API_KEY` in your `.env`
3. Check its type

- [ ] It is an **Algo Trading App**

If not, convert or recreate it there.

> **If you recreate it, the key and secret change.** Update `UPSTOX_API_KEY` and
> `UPSTOX_API_SECRET` in `.env` before going on.

### 2.4 Register the IP

**My Apps → your app → app settings**

- **Primary IP** = the address from 2.1
- **Secondary IP** = leave blank unless you have a second

- [ ] Registered
- [ ] Date written into the box above

> **Three things that will bite:**
> 1. Changeable **only once per calendar week**. Get it right.
> 2. Registering it **invalidates your current access token** — 2.5 is next for
>    that reason.
> 3. A wrong IP produces rejections whose message names the wrong cause.

### 2.5 Re-authorise — after 2.4, not before

**Admin & diagnostics → Upstox console → Renew Access (Web Login)**

- [ ] Console shows **Token Active** with an expiry later today

---

# Phase 3 — `.env` changes

Exactly three lines. Everything else stays as it is.

```sh
nano .env
```

```sh
APPLICATION_MODE=LIVE
LIVE_COMPLIANCE_APPROVED=true
LIVE_STATIC_IP_VERIFIED=true
```

```sh
docker compose up -d api scanner-worker
docker compose logs --tail=20 api      # confirm it restarted cleanly
```

- [ ] Three lines set
- [ ] API restarted without error

> **Do not touch `LIVE_TRADING_ENABLED`.** It is locked: the API refuses to
> start with it true, unconditionally. That is Phase 6 and it is mine.
>
> `APPLICATION_MODE=LIVE` while the lock is on is safe — it affects logging, the
> status display and one readiness gate, and changes no trading behaviour.
>
> `LIVE_COMPLIANCE_APPROVED` and `LIVE_STATIC_IP_VERIFIED` are **attestations**.
> The system cannot verify either and simply trusts you. Set them true only when
> they are true.

---

# Phase 4 — Configure the application

### 4.1 Settings → Trading controls

| Setting | Value |
|---|---|
| **Live broker** | `UPSTOX` — defaults to `NONE`, which sends nothing |
| **Approval mode** | `TELEGRAM_APPROVAL` — **not** `AUTOMATIC` |

- [ ] Both set and saved

Read the **Effective limits** panel at the top before moving on — it says what
these settings actually permit once read together, and names the binding control
when two of them disagree.

### 4.2 Strategies → set a square-off time

On **each** strategy card, under **How the trade is left**:

- **Square off at** → **15:15**

- [ ] Set on all four strategies

> **Do this before live, not after.** The shipped default closes a position only
> on its stop, its target, or the day's limit. **Nothing closes it because the
> session is ending.** Live, Upstox would square off your MIS position at its
> own time and its own price. The form flags its absence in amber until you fix
> it.

### 4.3 Telegram must work in both directions

With `TELEGRAM_APPROVAL`, an order the bot cannot deliver is **blocked** — not
left pending. That is correct fail-closed behaviour, and it means a broken
webhook produces **zero orders, silently**.

- [ ] **Settings → Alerts → Send test alert** arrives
- [ ] The inbound webhook is configured
- [ ] Your Telegram user ID is in the allowed list

### 4.4 Fund the account

- [ ] **₹10,000 and nothing more**
- [ ] The account holds **no existing positions**

> Live reconciliation blocks trading on any position it cannot explain, so
> existing holdings will stop the system arming at all. That is the safety
> behaving correctly and it is indistinguishable from the system being broken.

---

# Phase 5 — Test a real order (the lock is still on)

**This works today.** The dry-run script talks to Upstox directly and does not
go through the application's live gate, so it can prove placement works before
the lock comes off.

**Nothing before this step proves anything.** Reading the API tells you nothing
about whether you are *allowed* to place an order — that is a separate
permission, and only a placement settles it.

### 5.1 Stage 1 — reads only, cannot place anything

```sh
docker compose exec api python scripts/verify_upstox_orders.py
```

- [ ] The **outbound IP** it reports matches what you registered in 2.4
- [ ] The order book echoes our `tag`
- [ ] Positions parse
- [ ] The margin endpoints answer
- [ ] The two trade-report endpoints answer
- [ ] Nothing is flagged as a PROBLEM

> 🛑 **STOP** and fix anything flagged before going on.

### 5.2 Stage 2 — one real order, then cancelled

Pick the **cheapest** stock the system already subscribes to:

```sh
grep UPSTOX_SUBSCRIPTIONS .env
```

Take an `NSE_EQ|INE…` entry. **Not** an `NSE_INDEX|…` entry — an index cannot be
traded.

```sh
docker compose exec api python scripts/verify_upstox_orders.py \
    --place-test-order \
    --instrument-key "NSE_EQ|<paste yours>" \
    --price 1.00
```

It prints what it is about to do and makes you type `PLACE A REAL ORDER`.

> **Read the price before you confirm.** It is a real limit buy. Anywhere near
> the market and it *will* fill. ₹1.00 is safe for anything trading above ₹10.

| Result | What it means | Do |
|---|---|---|
| `Placement: confirmed` | Everything works | Send me the output |
| `[REFUSED]` | Not an Algo app (2.3), IP not registered (2.4), or funds — in that order of likelihood | Fix and re-run |
| `[UNKNOWN]` | The request may have reached the exchange | **Check the Upstox order book by hand before running anything else** |
| `tag-not-found` | Placement works, automatic recovery does not | Tell me — it changes what I build next |

- [ ] Result written into the box above
- [ ] **Confirmed in the Upstox app that nothing is left open**

> A cancellation request is not a cancellation. Look.

---

# Phase 6 — I remove the lock

Send me all three:

```sh
# 1
docker compose exec api alembic current

# 2 — the full output of Phase 5.2, not a summary

# 3
./scripts/live-control.sh status
```

I remove the `LIVE_TRADING_ENABLED` refusal in its own commit with the evidence
in the message. Then:

```sh
git pull origin main
docker compose build api scanner-worker && docker compose up -d
# set LIVE_TRADING_ENABLED=true in .env
docker compose up -d api scanner-worker
```

---

# Phase 7 — Every trading morning

```sh
cd "$SIDRA"

# 1. Token fresh — Upstox console says "Token Active"

# 2. Reconcile against the broker
./scripts/live-control.sh reconcile        # safe_to_trade must be TRUE

# 3. Arm — WITHIN 15 MINUTES of step 2
./scripts/live-control.sh arm "routine live session"

# 4. Start the scanner in the UI
```

**Two windows:**

- **A reconciliation is valid for 15 minutes.** Past that the gate fails and
  arming is refused. Reconcile and arm back to back.
- **Activation lapses after 8 hours** on its own. A system armed this morning is
  not still armed unattended tonight.

**At any time, if you are unsure about anything:**

```sh
./scripts/live-control.sh disarm
```

Disarming is always safe, always allowed, and instant — including when nothing
is armed.

### After the close

```sh
# Stop the scanner in the UI, then:
```

1. **History** → check the day
2. On the day's row, press **Fetch** to pull Upstox's own realised P&L and
   charges. They are recorded *beside* yours, never over them.
3. **Mismatch is a stop sign** — it means the fills are not what we recorded.
   Investigate before trading again.
4. **Estimated charges** is normal, not a fault. Upstox publishes no per-trade
   charge at all.

An 18:00 IST job does the fetch automatically on days with live orders.

---

## If arming is refused

The response names exactly which gates are blocking. **That message is the
checklist** — use it rather than this document, because it reads the real state.

| Gate | Fix |
|---|---|
| `runtime_mode` | Phase 3, then Phase 6 |
| `compliance` | `LIVE_COMPLIANCE_APPROVED=true` (Phase 3) |
| `static_ip` | `LIVE_STATIC_IP_VERIFIED=true` (Phase 3) |
| `service_health` | Postgres or Redis is down — `docker compose ps` |
| `broker_selected` | Settings → Trading controls → Live broker (Phase 4.1) |
| `external_reconciliation` | Run `reconcile`, or it is more than 15 minutes old |
| `administrator_activation` | Run `arm` |

---

## Other things that go wrong

**Upstox stops working, usually first thing in the morning.**
The token expired. The console says **No Active Token**. Renew it.

**Orders start failing one morning for no reason.**
Check 2.1 first. A dynamic IP that changed produces rejections whose message
names the wrong cause.

**No signals at all.**
Scanner & Universe → Scanner, read the rejection reasons. "Required market data
unavailable: rvol" is expected while the 1-minute history is still thin — the
RVOL baseline wants 10 sessions. That refusal is the system working.

**The API will not start.**
`docker compose logs api | tail -50`. The settings validator refuses with a
specific message. Most common: `LIVE_TRADING_ENABLED=true` (refused always), or
`AUTO_CREATE_SCHEMA=true` with `APP_ENV=production`.

---

## What this checklist does not cover

**Whether the strategies are worth trading.** They are not proven. The paper
sample we have was measured by a scorer that awarded full marks for *absent*
inputs — that is fixed, but it means the old numbers describe a system that no
longer exists and are not evidence for the current one in either direction.

The strategy detail pages will say **Not enough evidence**, and they are right
to. They need at least 30 resolved trades on each of out-of-sample backtest and
paper-forward before they will say anything, and their strongest verdict is
*promising, not proven*.

**The real next piece of work is the 1-minute history backfill** — roughly eight
sessions on the instance against ten needed for the RVOL baseline alone. Nothing
can be evaluated until that is done.

Going live before then is a legitimate way to prove the plumbing under real
conditions. Do it at the smallest size the exchange permits and treat every
rupee as tuition, not investment.
