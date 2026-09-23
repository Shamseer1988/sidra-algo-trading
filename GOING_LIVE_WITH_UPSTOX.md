# Going live with Upstox — the full runbook

You have a static IP, so the long-lead item is cleared. What follows is every
remaining step in the order it has to happen, with the command or the exact
screen for each one.

**Read this first, because it changes how you read the rest.** The last step —
step 10 — is a code change only I can make, and I have deliberately not made it
yet. `LIVE_TRADING_ENABLED` is locked shut in the settings validator: it refuses
the value outright, so an API given `LIVE_TRADING_ENABLED=true` today will not
start at all. That lock comes off in its own commit, after step 8 proves Upstox
will actually accept an order from you. Everything in steps 1–9 is safe,
reversible, and places no orders.

There is also a judgement in step 11 that I'd want you to read before funding
anything. It is not about the plumbing.

---

## Where things stand

| | |
|---|---|
| `main` | `8b341f8` — broker adapter, Upstox order client, per-order approval, daily P&L limits |
| Your NAS | `860880c`, schema `0018_backtest_sweep` |
| Gap | 17 commits, 4 migrations (0019 → 0022) |
| Live trading | Locked in code. Cannot be switched on by configuration. |

---

## Step 1 — Confirm the app may place orders

This is the single thing most likely to stop you, and it is invisible until an
order is rejected.

Your Upstox Developer App currently streams market data. **Placing an order is a
separate capability**, and Upstox now distinguishes an **Algo Trading App** from
an ordinary one. A data app streams quotes perfectly and refuses placements.

1. Sign in at <https://account.upstox.com/developer/apps>
2. Open the app whose API key is in your `.env` as `UPSTOX_API_KEY`
3. Check its type

**If it is not an Algo Trading App**, convert or recreate it there. Expect
Upstox to ask you to accept the API terms and confirm you understand the SEBI
algo rules.

> **If you recreate the app**, the API key and secret change. Update
> `UPSTOX_API_KEY` and `UPSTOX_API_SECRET` in `.env`, and re-authorise (step 6).

You cannot confirm this by reading the API. Step 8 confirms it by placing one
real order. Everything between here and there is preparation for that test.

---

## Step 2 — Register the static IP against the app

Since **1 April 2026** every API order must originate from an IP registered with
your broker in advance. Orders from any other address are rejected. This is
exchange-mandated; nothing in the code can work around it.

**First, find the address orders will actually leave from.** Run this **on the
NAS**, not on your laptop:

```sh
curl -s https://api.ipify.org
```

That is the address to register. Not the NAS's LAN address (192.168.x.x), not
your laptop's, not the router's admin page. If the NAS reaches the internet
through anything — a VPN, Tailscale exit node, a proxy — the address above is
what Upstox sees, and it is the one that counts.

Then in **My Apps → your app → app settings**, set:

- **Primary IP** — the address above (required)
- **Secondary IP** — optional failover; leave blank if you have only one

**Three constraints that will bite you:**

1. **You can change the registered IP only once per calendar week.** Get it
   right. If you register the wrong address, you are out until the next week.
2. **Changing the IP invalidates your existing access token.** You must
   re-authorise immediately afterwards — that is step 6, and it is why step 6
   comes after this one rather than before.
3. **Verify the IP really is static.** "Static" from an ISP sometimes means
   "sticky DHCP". Run the `curl` above once today and once in three days. If it
   changed, it is not static and you need to go back to the ISP before going
   further.

---

## Step 3 — SEBI algo registration: you do not need it

Registration with the exchange, and an Algo ID, is required only for an algo
placing **more than ten orders per second**. You are configured for two to three
trades per *day*. Nowhere near it. No registration, no Algo ID, no waiting.

If that ever changes, the application already supports it: set `UPSTOX_ALGO_NAME`
in the API environment to the exact name registered in Upstox My Apps
(case-sensitive) and it is sent as the `X-Algo-Name` header on every call. Leave
it unset until you genuinely have one — sending an unregistered name is worse
than sending none.

---

## Step 4 — Back up the database

Four migrations are about to run. Take a backup first. You already have the
script:

```sh
cd /volume1/docker/sidra-algo-trading     # adjust to your actual path
./scripts/backup-postgres.sh
```

Confirm it wrote a non-trivial file before continuing:

```sh
ls -lh backups/
```

If that script has never run on this NAS, run it now and read its output rather
than assuming. A backup you have not seen succeed is not a backup.

---

## Step 5 — Deploy 17 commits and 4 migrations

```sh
cd /volume1/docker/sidra-algo-trading
git fetch origin main
git log --oneline HEAD..origin/main | wc -l    # expect 17
git checkout main
git pull origin main
```

Rebuild and restart:

```sh
docker compose build api scanner-worker web
docker compose up -d
```

Run the migrations explicitly — do not rely on auto-create, which is off in
production by design:

```sh
docker compose exec api alembic upgrade head
docker compose exec api alembic current
```

`alembic current` must report **`0022_live_submission_broker`**. If it reports
anything else, stop and send me the output.

Then confirm the stack is healthy:

```sh
docker compose ps
docker compose logs --tail=50 api
```

**What the four migrations do**, so you know what changed under your data:

| | |
|---|---|
| `0019` | reconciliation findings table |
| `0020` | live shadow decisions table |
| `0021` | live order submissions and approvals |
| `0022` | `broker` column on submissions and approvals |

All four are additive. Nothing is dropped or rewritten, and `0022` downgrades
cleanly — I tested it both directions.

---

## Step 6 — Re-authorise Upstox

**Do this after step 2, not before.** Registering or changing the static IP
invalidates your current access token, and a system holding a dead token fails
at the worst possible moment.

Upstox tokens expire daily at **03:30 IST**, so this is a recurring fact of life
rather than a one-off.

- **Manual**: web UI → **Brokers → Upstox → Authorise**. It sends you to Upstox
  and the callback stores the token encrypted.
- **Automatic**: `UPSTOX_AUTO_AUTH_ENABLED=true` with `UPSTOX_MOBILE_NUMBER`,
  `UPSTOX_PIN` and `UPSTOX_TOTP_SECRET` set, and it renews itself each morning
  at 08:30 IST.

If you are already using auto-auth for market data, it covers order placement
too — same token, no second authorisation — *provided* step 1 is satisfied.

Confirm it worked: the Upstox panel should show a live token with an expiry
later today.

---

## Step 7 — Fund the account, and decide the number now

Put **₹10,000** in and nothing more.

The system is configured for a ₹10,000 account at 5× intraday leverage, with a
₹2,000 daily profit target and ₹1,000 daily loss limit, both enforced in code —
the day stops when either is hit. But those limits govern the *strategy's*
behaviour. They do not protect you from a bug, a stuck order, or an unresolved
submission. Fund only what you would accept losing entirely while the system is
new.

**Do not connect an account holding positions or funds you need.** Live
reconciliation blocks trading whenever it finds a position it cannot explain, so
existing holdings in the same account will simply stop the system from ever
arming. That is the safety behaving correctly, and it is indistinguishable from
the system being broken.

---

## Step 8 — The dry run: one real order, then cancel it

**This is the step that proves steps 1, 2 and 6 actually worked.** Nothing before
it does. Reading the API proves nothing about whether you may place an order —
that is a separate permission, and only a placement settles it.

The script is `apps/api/scripts/verify_upstox_orders.py`, and it runs in two
stages.

### Stage 1 — reads only, cannot place anything

```sh
docker compose exec api python scripts/verify_upstox_orders.py
```

It reports:

- **the outbound IP** — check this matches what you registered in step 2
- whether the order book echoes the `tag` we send (the key that recovers a lost
  order)
- whether positions parse
- whether the margin endpoints answer readably
- how a bad request classifies

Fix anything it flags before going on.

### Stage 2 — places one real order

First pick the instrument. Use one the system already subscribes to, so you know
the key is valid — they are listed in `UPSTOX_SUBSCRIPTIONS` in your `.env`:

```sh
grep UPSTOX_SUBSCRIPTIONS .env
```

Take any `NSE_EQ|INE…` entry from that list. Prefer the cheapest stock in it:
the test is a limit *buy*, so a cheap share is a smaller accident if it fills.
Do **not** use an `NSE_INDEX|…` entry — an index cannot be traded.

Then pick a limit price **far below** the current market, so the buy cannot
fill. A tenth of the market price is a reasonable rule; ₹1.00 is safe for
anything trading above ₹10.

```sh
docker compose exec api python scripts/verify_upstox_orders.py \
    --place-test-order \
    --instrument-key "NSE_EQ|INE669E01016" \
    --price 1.00
```

(Replace the key with yours — the one above is a format example, not a
recommendation, and I have not verified it against your subscription list.)

It prints exactly what it is about to do, then asks you to type
`PLACE A REAL ORDER` before sending anything. It then places one buy for one
share, looks it up in the order book by our tag, and cancels it.

**Read the price you passed before you type the confirmation.** It is a real
limit buy. If the price is anywhere near the market, it *will* fill. One share of
IDEA is a rounding error, but the habit matters more than the amount.

**What each outcome means:**

| Result | What it means |
|---|---|
| `Placement: confirmed` | Everything works. Send me the output and I'll do step 10. |
| `[REFUSED]` | The app is not an Algo Trading App (step 1), or the IP is not registered (step 2), or insufficient funds. In that order of likelihood. |
| `[UNKNOWN]` | The request may have reached the exchange. **Check your Upstox order book by hand before running anything else.** |
| `tag-not-found` | Placement works but automatic recovery does not. Tell me — it changes what I build next. |

Whatever happens, **confirm in the Upstox app that nothing is left open.** A
cancellation request is not a cancellation.

---

## Step 9 — Set the application's own controls

These live in the web UI and the API environment, deliberately separate so no
single change turns live trading on.

**In the web UI, under System → Settings** (admin only):

| Setting | Value | Why |
|---|---|---|
| **Live broker** | `UPSTOX` | Defaults to `NONE`, which sends nothing. Nothing else matters until this is set. |
| **Approval mode** | `TELEGRAM_APPROVAL` | Start here, **not** `AUTOMATIC`. Every order asks you on Telegram first and expires unanswered after 3 minutes. |

**In `.env` on the NAS**, then `docker compose up -d api scanner-worker`:

```sh
APPLICATION_MODE=LIVE
LIVE_COMPLIANCE_APPROVED=true
LIVE_STATIC_IP_VERIFIED=true
```

`LIVE_STATIC_IP_VERIFIED` is a claim about the world and the system trusts it.
Set it only once step 2 is genuinely done and step 8's stage 1 showed the
matching address.

Leave `LIVE_TRADING_ENABLED` alone. It is locked; setting it true stops the API
from starting.

**Then, under System → Live Gates:**

1. **Reconcile** — must come back clean, and must have run within the last 15
   minutes for any order to be authorised
2. **Activate** — arms the system; expires on its own after 8 hours, so a system
   armed this morning is not still armed unattended tonight

The **Live Gates** page lists every gate and what is blocking each one, including
"no live broker is selected". Use it as the checklist rather than this document —
it reads the real state.

---

## Step 10 — I remove the lock

Send me:

- the full output of step 8 stage 2
- `docker compose exec api alembic current`
- a screenshot of the **Live Gates** page

I will then remove the `LIVE_TRADING_ENABLED` refusal in its own commit, with
the evidence in the message. That is the correct moment: after a real order has
been accepted and cancelled, not before. You deploy that one commit, set
`LIVE_TRADING_ENABLED=true`, restart, and the readiness gates go green.

The first live order will then be a Telegram message asking your permission.

---

## Step 11 — The thing I would not do yet

Everything above is plumbing. This is about money, so I want to be plain.

**The current strategies have no edge, and going live with them will lose money
at a predictable rate.**

The paper journal through 23 September:

- **22 wins in 55 trades = 0.400.** A coin flip targeting 1.5× reward-to-risk
  produces exactly 1/(1+1.5) = 0.400. The strategies are indistinguishable from
  random entry.
- **26 of 33 losers never moved half a unit of risk in your favour.** Average
  loser MFE was 0.35 R. These are not near-misses.
- **Costs are 0.41 R per trade** measured from actual fills. Even the best
  scoring band (71–77, +0.13 R gross) is net negative.

None of that is fixed by a setting, an account size, or a broker. It is fixed by
finding an edge — and finding one needs history the system does not have:
roughly eight sessions of 1-minute data per instrument, which is far too little
to test anything against.

**The sequence I would follow:**

1. Steps 1–10 above. The plumbing should be ready and proven. ✅ in progress
2. **Backfill 1-minute history properly** so the strategy sweep has something to
   work with. This is the actual blocker and I'd start it next.
3. Find and validate an edge on that history.
4. *Then* fund the account and trade it small.

Steps 1–2 can run in parallel. Going live before step 3 means paying real
brokerage to re-learn what the paper journal already told you for free.

If you want to go live anyway — to prove the plumbing under real conditions —
then do it at the smallest size the exchange permits and treat every rupee as
tuition, not investment. That is a legitimate choice. It is just a different one
from trading a strategy you expect to make money.

---

## Quick reference

**Commands, in order:**

```sh
# Step 2 — on the NAS
curl -s https://api.ipify.org

# Step 4
./scripts/backup-postgres.sh && ls -lh backups/

# Step 5
git fetch origin main && git checkout main && git pull origin main
docker compose build api scanner-worker web && docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api alembic current          # expect 0022_live_submission_broker

# Step 8
grep UPSTOX_SUBSCRIPTIONS .env                   # pick a cheap NSE_EQ key from this
docker compose exec api python scripts/verify_upstox_orders.py
docker compose exec api python scripts/verify_upstox_orders.py \
    --place-test-order --instrument-key "<your NSE_EQ key>" --price 1.00
```

**Send me when done:** step 8's full output, `alembic current`, and the Live
Gates screenshot.

**If anything fails**, send me the command and its complete output rather than a
summary. Most of these failures produce messages that name the wrong cause.
