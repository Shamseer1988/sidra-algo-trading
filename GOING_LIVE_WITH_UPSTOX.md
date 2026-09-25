# Going live with Upstox — the full runbook

For an existing deployment. Your NAS is already running an older build; this
takes it to current and then through every step needed to place real orders.

Times are IST. The NSE regular session is 09:15–15:30.

---

## Read this first

**The last step is a code change only I can make, and I have deliberately not
made it.** `LIVE_TRADING_ENABLED` is refused by the settings validator —
*unconditionally*, not conditionally on any attestation you set. An API given
`LIVE_TRADING_ENABLED=true` today does not start at all.

That lock comes off in its own commit, after Step 8 proves Upstox will actually
accept an order from you. **Everything in Steps 0–9 is safe, reversible, and
places no orders.**

There is a judgement in Step 12 about whether you should do this at all. It is
not about the plumbing. Please read it before you fund anything.

---

## The shape of the thing

Nine gates must all pass before the system will arm, and arming is itself the
ninth. They are deliberately spread across three places so that no single change
— no single mistaken keystroke, no single compromised surface — can turn live
trading on.

| Gate | Where it is satisfied | Step |
|---|---|---|
| `runtime_mode` | `.env` on the NAS, after I remove the lock | 10 |
| `compliance` | `.env` — your attestation | 9 |
| `static_ip` | `.env` — your attestation | 9 |
| `service_health` | Postgres and Redis up | automatic |
| `broker_selected` | Settings → Trading controls | 9 |
| `broker_adapter` | Present in code | automatic |
| `live_risk_engine` | Present in code | automatic |
| `external_reconciliation` | A clean reconcile, **less than 15 minutes old** | 11 |
| `administrator_activation` | You arm it, expires after 8 hours | 11 |

Two of these are attestations — `compliance` and `static_ip`. **The system
trusts you.** It cannot verify either. Setting them to true when they are not
true is lying to your own safety system.

---

## Step 0 — Find out where you actually are

Do not trust my notes about your NAS. Ask it.

```sh
cd /volume1/docker/sidra-algo-trading     # adjust to your real path
git log --oneline -1
docker compose exec api alembic current
docker compose ps
```

Write down the three answers. The migration revision is the one that matters —
it tells you how much schema change is coming.

If `alembic current` reports `0018_backtest_sweep`, eight migrations are due. If
it reports something later, fewer. Either way the upgrade procedure in Step 5 is
the same.

---

## Step 1 — Confirm the app may place orders

**This is the single thing most likely to stop you, and it is invisible until an
order is rejected.**

Your Upstox Developer App currently streams market data. **Placing an order is a
separate capability.** Upstox distinguishes an **Algo Trading App** from an
ordinary one, and a data app streams quotes perfectly while refusing placements.

1. Sign in at <https://account.upstox.com/developer/apps>
2. Open the app whose key is in `.env` as `UPSTOX_API_KEY`
3. Check its type

If it is not an Algo Trading App, convert or recreate it there. Expect Upstox to
ask you to accept the API terms and confirm you understand the SEBI algo rules.

> **If you recreate the app the key and secret change.** Update
> `UPSTOX_API_KEY` and `UPSTOX_API_SECRET` in `.env`, and re-authorise (Step 6).

You cannot confirm this by reading the API. Step 8 confirms it by placing one
real order.

---

## Step 2 — The static IP

Since **1 April 2026**, every API order must originate from an IP registered
with your broker in advance. Orders from any other address are rejected. This is
exchange-mandated; nothing in this code can work around it.

### 2a. Find the address orders will actually leave from

Run this **on the NAS**, from inside the API container — not on your laptop, not
on the NAS shell if you can avoid it. The container is what will talk to Upstox,
so the container's view is the one that counts.

```sh
docker compose exec api python -c "import httpx; print(httpx.get('https://api.ipify.org').text)"
```

If that fails, the NAS shell is close enough for a first look:

```sh
curl -s https://api.ipify.org
```

That address is what you register. **Not** the NAS's LAN address
(`192.168.x.x`), not your laptop's, not what your router's admin page calls your
WAN IP, and not what a "what is my IP" site tells you from your phone.

### 2b. Rule out the things that silently change it

This matters more for you than for most people, because your NAS is already
reachable through Cloudflare Tunnel and Tailscale.

| Thing | Effect on outbound orders |
|---|---|
| **Cloudflare Tunnel** | **None.** It is inbound only. It does not change the address your outbound API calls leave from. |
| **Tailscale, plain** | **None.** Peer-to-peer traffic only. |
| **Tailscale with an exit node enabled** | **Breaks it.** All outbound traffic leaves via the exit node's address, not yours. |
| **Any VPN on the NAS** | **Breaks it**, same reason. |
| **CGNAT from your ISP** | **Breaks it.** You share an address with other customers and it can change. |

If the `ipify` result does not match the WAN address you expect, something in
that list is in the path. Find it before registering anything.

### 2c. Prove it is genuinely static

"Static" from an ISP sometimes means "sticky DHCP" — an address that survives a
reboot and then changes one Tuesday in November.

Run the check now, then again in three days, then again after deliberately
rebooting the router:

```sh
curl -s https://api.ipify.org
```

Three identical answers including one across a reboot is reasonable evidence.
One reboot that changes it means you do not have a static IP, whatever the ISP
called it, and you should stop here and go back to them.

### 2d. Register it

In **My Apps → your app → app settings**:

- **Primary IP** — the address from 2a (required)
- **Secondary IP** — optional failover; leave blank if you have only one

**Three constraints that will bite you:**

1. **You can change the registered IP only once per calendar week.** Get it
   right. Register the wrong address and you are out until the next week.
2. **Changing the registered IP invalidates your existing access token.** You
   must re-authorise immediately afterwards — that is Step 6, and it is why
   Step 6 comes after this one rather than before.
3. **A dynamic IP that changes silently produces rejections that look like
   something else.** The error will not say "wrong IP" clearly. If orders start
   failing one morning for no reason, check 2a first.

---

## Step 3 — SEBI algo registration: you do not need it

Registration with the exchange, and an Algo ID, is required only for an algo
placing **more than ten orders per second**. You are configured for four trades
per *day*. Nowhere near it.

If that ever changes, the application already supports it: set `UPSTOX_ALGO_NAME`
to the exact name registered in Upstox My Apps (case-sensitive) and it is sent
as `X-Algo-Name` on every call. **Leave it unset until you genuinely have one** —
sending an unregistered name is worse than sending none.

---

## Step 4 — Back up the database

Several migrations are about to run, and the database holds every signal, order,
fill and settings revision you have accumulated.

```sh
cd /volume1/docker/sidra-algo-trading
./scripts/backup-postgres.sh
ls -lh backups/
```

Confirm it wrote a non-trivial file. **A backup you have not seen succeed is not
a backup.** If that script has never run on this NAS, run it now and read its
output rather than assuming.

---

## Step 5 — Deploy the current build

```sh
cd /volume1/docker/sidra-algo-trading
git fetch origin main
git log --oneline HEAD..origin/main | wc -l    # how far behind you are
git checkout main
git pull origin main
```

Rebuild and restart:

```sh
docker compose build api scanner-worker web
docker compose up -d
```

Run the migrations explicitly. The image does **not** run them on start, and
auto-create is off in production by design:

```sh
docker compose exec api alembic upgrade head
docker compose exec api alembic current
```

`alembic current` must report **`0026_broker_day_snapshots`**. Anything else,
stop and send me the output.

**What the migrations do**, so you know what changed under your data:

| | |
|---|---|
| `0019` | structured findings on reconciliations |
| `0020` | live shadow decisions |
| `0021` | live activation, submission and approval records |
| `0022` | which broker a submission belongs to |
| `0023` | the day a profit target or loss limit was reached |
| `0024` | which mode a halt stopped — paper's day or the broker's |
| `0025` | every saved version of a settings row |
| `0026` | what the broker said a day was worth |

All eight are additive. Nothing is dropped or rewritten. I ran all 26 against an
empty database before writing this: 37 tables, ending at head.

Then confirm health:

```sh
docker compose ps
docker compose logs --tail=50 api
```

> **The navigation changed in this build.** Seven sections now — Dashboard,
> Strategies, Scanner & Universe, Orders & Positions, History, Risk, Settings —
> with everything diagnostic under a collapsed **Admin & diagnostics**. If you
> go looking for "Live Gates" or "Risk Center", they are **Admin &
> diagnostics → Live readiness** and **Risk**.

---

## Step 6 — Re-authorise Upstox

**Do this after Step 2, not before.** Registering or changing the static IP
invalidates your current access token.

Upstox tokens expire daily at about **03:30 IST**, so this is a recurring fact
of life rather than a one-off.

- **Manual:** Admin & diagnostics → Upstox console → **Renew Access (Web Login)**
- **Automatic:** `UPSTOX_AUTO_AUTH_ENABLED=true` with `UPSTOX_MOBILE_NUMBER`,
  `UPSTOX_PIN` and `UPSTOX_TOTP_SECRET` set. Renews at 08:30 IST on weekdays.

If you already use auto-auth for market data it covers order placement too —
same token, no second authorisation — *provided* Step 1 is satisfied.

Confirm: the console should show **Token Active** with an expiry later today.

---

## Step 7 — Fund the account, and decide the number now

Put **₹10,000** in and nothing more.

The system is configured for a ₹10,000 account at 5× intraday leverage, with a
₹2,000 daily profit stop and a ₹400 or ₹1,000 daily loss stop depending on your
profile — both enforced in code, both ending the day when reached. Those limits
govern the *strategy's* behaviour. **They do not protect you from a bug, a stuck
order, or an unresolved submission.** Fund only what you would accept losing
entirely while the system is new.

**Do not connect an account holding positions or funds you need.** Live
reconciliation blocks trading whenever it finds a position it cannot explain, so
existing holdings in the same account will stop the system arming at all. That
is the safety behaving correctly, and it is indistinguishable from the system
being broken.

---

## Step 8 — The dry run: one real order, then cancel it

**This is the step that proves Steps 1, 2 and 6 actually worked.** Nothing
before it does. Reading the API proves nothing about whether you may place an
order — that is a separate permission, and only a placement settles it.

### Stage 1 — reads only, cannot place anything

```sh
docker compose exec api python scripts/verify_upstox_orders.py
```

It reports:

- **the outbound IP** — check this matches what you registered in Step 2
- whether the order book echoes the `tag` we send (the key that recovers a lost
  order)
- whether positions parse
- whether the margin endpoints answer readably
- whether the two **trade-report** endpoints answer — these are what the History
  screen reconciles against, and this is where the date format and financial
  year get confirmed against your real account
- how a bad request classifies

Fix anything it flags before going on.

### Stage 2 — places one real order

Pick an instrument the system already subscribes to, so you know the key is
valid:

```sh
grep UPSTOX_SUBSCRIPTIONS .env
```

Take any `NSE_EQ|INE…` entry. **Prefer the cheapest stock in the list** — the
test is a limit *buy*, so a cheap share is a smaller accident if it fills. Do
**not** use an `NSE_INDEX|…` entry; an index cannot be traded.

Then pick a limit price **far below** the current market so the buy cannot fill.
A tenth of the market price is a reasonable rule; ₹1.00 is safe for anything
trading above ₹10.

```sh
docker compose exec api python scripts/verify_upstox_orders.py \
    --place-test-order \
    --instrument-key "NSE_EQ|INE669E01016" \
    --price 1.00
```

(Replace the key with one from *your* list. The one above is a format example, not
a recommendation.)

It prints exactly what it is about to do, then requires you to type
`PLACE A REAL ORDER` before sending anything. It places one buy for one share,
finds it in the order book by our tag, and cancels it.

**Read the price you passed before you type the confirmation.** It is a real
limit buy. If the price is anywhere near the market, it *will* fill.

| Result | What it means |
|---|---|
| `Placement: confirmed` | Everything works. Send me the output and I'll do Step 10. |
| `[REFUSED]` | Not an Algo Trading App (Step 1), or the IP is not registered (Step 2), or insufficient funds. In that order of likelihood. |
| `[UNKNOWN]` | The request may have reached the exchange. **Check your Upstox order book by hand before running anything else.** |
| `tag-not-found` | Placement works but automatic recovery does not. Tell me — it changes what I build next. |

Whatever happens, **confirm in the Upstox app that nothing is left open.** A
cancellation request is not a cancellation.

---

## Step 9 — Configure the application

Two places, deliberately separate.

### In the web UI — Settings → Trading controls (admin only)

| Setting | Value | Why |
|---|---|---|
| **Live broker** | `UPSTOX` | Defaults to `NONE`, which sends nothing. Nothing else matters until this is set. |
| **Approval mode** | `TELEGRAM_APPROVAL` | Start here, **not** `AUTOMATIC`. Every order asks you on Telegram first and expires unanswered after 3 minutes. |

While you are in Settings, check the rest of the profile — the **Effective
limits** panel at the top tells you what these settings actually permit once
read together, and names the binding control when two of them disagree.

### In the Strategies screen — set a square-off time

**Do this before live trading, not after.** The shipped default closes a
position only on its stop, its target, or the day's limit. **Nothing closes it
because the session is ending.** Live, Upstox would square off your MIS position
at its own time and its own price.

On each strategy card, under **How the trade is left**, set **Square off at** to
**15:15**. The form flags its absence in amber until you do.

### In `.env` on the NAS

```sh
LIVE_COMPLIANCE_APPROVED=true
LIVE_STATIC_IP_VERIFIED=true
```

Then `docker compose up -d api scanner-worker`.

`LIVE_STATIC_IP_VERIFIED` is a claim about the world and the system trusts it.
Set it only once Step 2 is genuinely done — including 2c — and Step 8's stage 1
showed the matching address.

**Leave `LIVE_TRADING_ENABLED` alone.** It is locked; setting it true stops the
API from starting.

**Leave `APPLICATION_MODE` at `PAPER` too.** Release 1 refuses that value the
same way: `prohibit_implicit_live_mode` in `apps/api/app/core/config.py` raises
during settings construction, so the API crash-loops rather than starting in a
degraded mode. Note the consequence — `live_readiness.py` wants
`APPLICATION_MODE == "LIVE"` for its runtime-mode gate, so that one gate stays
red until both refusals are lifted together at activation. It is not a
misconfiguration on your side and there is no `.env` value that clears it.

---

## Step 10 — I remove the lock

Send me:

- the full output of Step 8 stage 2
- `docker compose exec api alembic current`
- `./scripts/live-control.sh status` (see Step 11)

I will then remove the `LIVE_TRADING_ENABLED` refusal in its own commit, with
the evidence in the message. That is the correct moment: **after** a real order
has been accepted and cancelled, not before.

You deploy that one commit, set `LIVE_TRADING_ENABLED=true`, restart, and the
remaining gates go green.

---

## Step 11 — Arming, each trading day

Two of the nine gates are not configuration — they have to be done fresh, and
one of them expires.

**These have no buttons in the web terminal.** The Live readiness screen reports
the gates; it does not drive them. Use the script:

```sh
./scripts/live-control.sh status                       # what the gates say
./scripts/live-control.sh reconcile                    # compare against the broker
./scripts/live-control.sh arm "first live session"     # arm for 8 hours
./scripts/live-control.sh disarm                       # revoke immediately
```

It asks for your admin email and password, and every action is audit-logged
under your user.

**The morning sequence:**

1. Token fresh (Step 6).
2. `./scripts/live-control.sh reconcile` — `safe_to_trade` must be **true**.
3. `./scripts/live-control.sh arm "…"` — **within 15 minutes** of the reconcile.
   The reconciliation goes stale after that and arming is refused.
4. Start the scanner.

**About the two windows:**

- **Reconciliation is valid for 15 minutes.** Not a suggestion — the gate
  fails past it. Reconcile, then arm, back to back.
- **Activation expires after 8 hours** (`LIVE_ACTIVATION_TTL_MINUTES=480`). It
  lapses on its own, so a system armed this morning is not still armed
  unattended tonight. You do not need to disarm at the close, though disarming
  is free and instant.

**Disarming is always safe and always allowed, including when nothing is armed.
If you are unsure about anything at all, disarm.**

**If arming is refused**, the response names exactly which gates are blocking.
That message is the checklist — use it rather than this document, because it
reads the real state.

---

## Step 12 — The thing I would not do yet

Everything above is plumbing. This is about money, so I want to be plain.

**I cannot tell you these strategies have an edge, and the last time we had
numbers they did not.**

The paper journal through 23 September showed 22 wins in 55 trades — 0.400,
which is exactly what a coin flip targeting 1.5× reward-to-risk produces. Costs
were 0.41 R per trade measured from actual fills.

**But that sample was measured by a scorer that was broken.** Four of the five
scoring sections awarded *full marks when their input was absent*. An instrument
with no VWAP, RVOL, regime or relative-strength data collected 60 of 100 points
for free. That is fixed — a missing required input now blocks the signal — but
it means the old numbers describe a system that no longer exists. They are not
evidence for the current one, in either direction.

**So there is currently no evidence either way, and that is worse than bad
evidence.** The strategy detail pages will say **Not enough evidence** and they
are right to: they need at least 30 resolved trades on each of out-of-sample
backtest and paper-forward before they will say anything, and even then their
strongest verdict is *promising, not proven*.

**The sequence I would follow:**

1. Steps 0–11 above. Get the plumbing ready and proven.
2. **Backfill 1-minute history properly.** This is the real blocker — the
   instance holds roughly eight sessions, and the RVOL baseline alone wants ten.
   A sweep has nothing to work with until this is done.
3. Rebuild the paper record under the fixed scorer. Find and validate an edge.
4. *Then* fund the account and trade it small.

Steps 1–2 can run in parallel. **Going live before step 3 means paying real
brokerage to re-learn what a paper journal would tell you for free.**

If you want to go live anyway — to prove the plumbing under real conditions —
that is a legitimate choice. Do it at the smallest size the exchange permits and
treat every rupee as tuition, not investment. It is just a different decision
from trading a strategy you expect to make money.

---

## After the first live day

1. **History → the day's row → Fetch.** Pulls Upstox's own realised P&L and
   charges and records them *beside* yours. The reconciliation column will say
   **Matched**, or **Mismatch** with both figures shown.
2. **Mismatch is a stop sign.** It means the fills are not what we recorded.
   Investigate before trading again.
3. A charge difference on its own stays **Estimated charges** — our charges are
   admittedly a local model, and Upstox publishes no per-trade figure at all.

An 18:00 IST job does the fetch automatically on days with live orders.

---

## Quick reference

```sh
# Step 0 — where am I
git log --oneline -1
docker compose exec api alembic current

# Step 2 — the address that matters
docker compose exec api python -c "import httpx; print(httpx.get('https://api.ipify.org').text)"

# Step 4
./scripts/backup-postgres.sh && ls -lh backups/

# Step 5
git fetch origin main && git checkout main && git pull origin main
docker compose build api scanner-worker web && docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api alembic current          # expect 0026_broker_day_snapshots

# Step 8
grep UPSTOX_SUBSCRIPTIONS .env                   # pick a cheap NSE_EQ key
docker compose exec api python scripts/verify_upstox_orders.py
docker compose exec api python scripts/verify_upstox_orders.py \
    --place-test-order --instrument-key "<your NSE_EQ key>" --price 1.00

# Step 11 — every trading morning
./scripts/live-control.sh reconcile
./scripts/live-control.sh arm "routine live session"
# and whenever you are unsure:
./scripts/live-control.sh disarm
```

**Send me when done:** Step 8's full output, `alembic current`, and
`./scripts/live-control.sh status`.

**If anything fails**, send the command and its *complete* output rather than a
summary. Most of these failures produce messages that name the wrong cause.
