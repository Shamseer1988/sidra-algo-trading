# Sidra — from a fresh clone to a running paper session

Written for the Synology NAS deployment, but nothing here is Synology-specific:
it is Docker Compose, a `.env` file, and a browser.

Read the two sentences below before anything else, because they decide what
this document is for.

**This sets up paper trading.** Every order it places is simulated. No module
reachable from these steps can send an order to a broker.

**Live trading is a separate, later, deliberate act.** The switch is held shut
by `LIVE_TRADING_ENABLED`, which the API refuses to start with set —
unconditionally, not conditionally on anything you configure here. Turning it on
is `GOING_LIVE_WITH_UPSTOX.md`, and it comes after a paper record exists, not
before.

Times are IST throughout. The NSE regular session is 09:15–15:30.

---

## Before you start

| You need | Why |
|---|---|
| Docker and Docker Compose v2 | Everything runs in containers |
| ~4 GB free disk | Postgres, Redis, two images, and the candle history |
| An Upstox account with a Developer App | The market-data feed |
| A Telegram bot (optional) | Alerts and, later, per-order approvals |

You do **not** need a static IP for paper trading. That is a live-trading
requirement and is covered in the going-live guide.

---

## Step 1 — Get the code

```sh
git clone https://github.com/Shamseer1988/sidra-algo-trading.git
cd sidra-algo-trading
```

On an existing checkout, take the current `main` instead:

```sh
git fetch origin main
git checkout main
git pull origin main
```

---

## Step 2 — Write the `.env`

```sh
cp .env.example .env
```

Then open `.env` and change the following. Everything else can stay as it ships.

**Secrets — replace all three:**

```sh
# A long random string. The API refuses to start in production with the placeholder.
JWT_SECRET=$(openssl rand -hex 32)

# The database password, in two places that must match:
POSTGRES_PASSWORD=<a strong password>
DATABASE_URL=postgresql+asyncpg://intraday_sentinel:<the same password>@postgres:5432/intraday_sentinel

# Only needed if you will use browser-based Upstox OAuth renewal:
UPSTOX_TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

**Environment:**

```sh
APP_ENV=development        # see the note below before changing this
APPLICATION_MODE=PAPER
LIVE_TRADING_ENABLED=false # leave it
AUTO_CREATE_SCHEMA=false   # see Step 3
```

> **On `APP_ENV`.** Setting it to `production` turns on four refusals at
> startup: `COOKIE_SECURE` must be true, `AUTO_CREATE_SCHEMA` must be false,
> `WEB_ORIGIN` must be HTTPS, and `JWT_SECRET` must not be a placeholder. Those
> are the right rules, and they mean you cannot run `production` over plain
> HTTP. Use `development` with the LAN HTTPS overlay (Step 8) until you have a
> real certificate, and understand that `development` is a weaker posture on a
> machine other people can reach.

**Reaching the terminal from another device on your LAN** — set these now, they
are used in Step 8:

```sh
LAN_HTTPS_HOST=192.168.x.y          # the NAS's LAN address
LAN_HTTPS_PORT=8443
WEB_BIND_HOST=127.0.0.1             # keep the plain-HTTP port loopback-only
WEB_ORIGIN=https://192.168.x.y:8443
PUBLIC_APP_URL=https://192.168.x.y:8443
```

**Upstox** — from your Developer App at <https://account.upstox.com/developer/apps>:

```sh
UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_REDIRECT_URI=https://192.168.x.y:8443/upstox/callback
UPSTOX_SUBSCRIPTIONS=NSE_INDEX|Nifty 50,NSE_EQ|INE002A01018,NSE_EQ|INE467B01029
UPSTOX_NIFTY_BENCHMARK_KEY=NSE_INDEX|Nifty 50
```

The redirect URI must match what you registered in the Developer App exactly,
character for character.

> **Keep NIFTY in the subscription list.** Two of the four strategies need it —
> the market-regime score and relative strength are both measured against it.
> Without it those inputs are missing, and a missing required input now
> *blocks* a signal rather than scoring it as though it were fine.

**Indicator periods** — the eight values near the bottom of `.env`
(`CANDLE_TIMEFRAME_SECONDS`, `OPENING_RANGE_MINUTES`, the EMA and ATR periods,
and so on) are a **fallback only**. Leave them alone. Once you save them in the
UI (Step 6) they live in the database and this file is never read for them
again. The Settings screen tells you which source is in use.

---

## Step 3 — Start the database and create the schema

```sh
docker compose up -d postgres redis
docker compose build api web
docker compose up -d api
```

The image does **not** run migrations on start. Run them yourself, once:

```sh
docker compose exec api alembic upgrade head
docker compose exec api alembic current   # should end at 0026_broker_day_snapshots
```

If `alembic current` prints nothing, the API cannot reach the database — check
that `POSTGRES_PASSWORD` and the password inside `DATABASE_URL` are the same
string.

---

## Step 4 — Create your login

```sh
docker compose exec api intraday-sentinel create-admin --email you@example.com
```

It prompts for a password twice. Minimum twelve characters. This is the only
account that exists; there is no default login and no signup page.

---

## Step 5 — Bring up everything else

```sh
docker compose up -d
docker compose ps
```

Five services should be healthy: `postgres`, `redis`, `api`, `scanner-worker`,
`web`. If you are using the LAN HTTPS overlay, see Step 8 first — it adds a
sixth.

Open <http://127.0.0.1:3001> on the NAS itself and sign in.

---

## Step 6 — Set the trading profile

Everything in this step is done in the browser. Nothing here needs a file edit.

**Settings → Trading controls.**

Start from a profile rather than typing twenty-one numbers. Two are provided:

| Profile | Risk a trade | Daily loss stop | Daily profit stop |
|---|---|---|---|
| **Cautious paper start** | ₹100 | ₹400 | ₹2,000 |
| **User advanced profile** | ₹250 | ₹1,000 | ₹2,000 |

Start on **Cautious paper start**. Applying a profile is validated, versioned
and audited exactly like a hand edit; raising a limit asks you to confirm first.

Read the **Effective limits** panel above the form before moving on. It is the
answer to "what do these settings actually permit once they are read together",
and it will tell you when two controls disagree — for instance when the daily
risk budget allows fewer trades than the trade ceiling does.

Four numbers are worth understanding, because their names do not fully explain
them:

- **Account capital ₹10,000** — the figure risk is sized from.
- **Exposure ceiling ₹50,000** — capital × leverage. This is *exposure*, not
  capital. You are not risking ₹50,000; you can be holding that much stock.
- **Four trades a day, account-wide** — counted on an entry order's **first
  fill**. A signal that never filled costs you nothing from the budget, and
  partial fills of one entry count once.
- **The ₹2,000 profit stop and the ₹400 loss stop end the day.** Not "filter the
  next signal" — end it. When either is reached the day is recorded as halted in
  the database, open positions are flattened, and nothing reopens it: not a
  restart, not reloading the UI, not a Redis flush, not editing these settings.
  Paper and live halt independently.

**Settings → Indicator periods.**

The banner at the top says `ENVIRONMENT` until you save once, and `DATABASE`
after. Press **Save** even if you change nothing — that is what moves these out
of `.env` for good.

One value to check: **RVOL baseline sessions** defaults to 10, and the relative
volume input is unavailable until you have that many sessions of 1-minute
history. Until then, any strategy that requires RVOL will refuse to signal and
say so. That refusal is correct. Lower it to 5 if you want signals sooner, and
understand you are comparing against a thinner baseline.

**Settings → Alerts.** Send a test alert if you configured a Telegram bot.

---

## Step 7 — Connect the market data

**Admin & diagnostics → Upstox console.**

1. **Set as Primary Feed** — makes Upstox the market-data source. (The same
   choice is on Settings → Market data; either works.)
2. **Renew Access (Web Login)** — opens Upstox, you log in, and it redirects
   back to the callback URL.
3. **Refresh Scrip Master** — once, so instrument tokens resolve to readable
   names across the app.

The access token is short-lived, roughly one trading day, so step 2 has to
happen every morning. Two ways:

- **Manual:** press **Renew Access (Web Login)** each morning.
- **Automatic:** fill `UPSTOX_MOBILE_NUMBER`, `UPSTOX_PIN`, `UPSTOX_TOTP_SECRET`
  and set `UPSTOX_AUTO_AUTH_ENABLED=true`, then restart the API. A scheduler job
  renews it at 08:30 IST on weekdays. The same console card shows whether it
  ran and when the token expires.

---

## Step 8 — LAN HTTPS (only if you want other devices to reach it)

Browsers treat a plain-HTTP LAN address as an insecure context, which breaks
parts of the UI. The overlay serves the terminal over HTTPS with a certificate
from Caddy's internal CA.

With the five `.env` values from Step 2 already set:

```sh
docker compose -f docker-compose.yml -f docker-compose.lan-https.yml up -d
```

Then trust Caddy's root CA on each device that will connect. It is inside the
`caddy_lan_data` volume at `/data/caddy/pki/authorities/local/root.crt`:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./sidra-root.crt
```

To avoid typing both `-f` flags forever, set `COMPOSE_FILE` in `.env`:

```sh
COMPOSE_FILE=docker-compose.yml:docker-compose.lan-https.yml
```

---

## Step 9 — Check the strategies before you start

**Strategies.** Four ship enabled. For each one, press **Details** and read
three things:

1. **What it does not do.** Most disappointment with a strategy is a
   disagreement about what it was supposed to do.
2. **Required inputs.** A missing required input blocks the signal. If you see
   no signals at all, this panel plus the scanner's data quality is where the
   answer is.
3. **The exit plan.** In particular the last line.

> **Set a square-off time before you trust the paper record.** The shipped
> default has none: a position is held until its stop or target is hit, or the
> day's limit flattens it, and nothing closes it because the session is ending.
> Live, your broker would square off an intraday position at its own time and
> its own price. A paper journal that holds positions past 15:30 is recording
> trades that could not have happened.
>
> On each strategy card, under **How the trade is left**, set **Square off at**
> to **15:15**. Both the form and the detail page flag its absence in amber
> until you do.

While you are there, decide on trailing. The default is none — the stop stays
where it was placed. `Move to entry once ahead` is the conservative first change
if you want one.

---

## Step 10 — Run a session

**Dashboard → Start scanner**, or **Scanner & Universe → Scanner**.

During the session:

| Screen | What it answers |
|---|---|
| **Dashboard** | Is everything up, is the scanner running, is the perimeter intact |
| **Scanner & Universe** | What is being watched, and what was rejected and why |
| **Orders & Positions** | What is open right now |
| **Risk** | How much of today's budget is left |
| **History** | What actually happened |

The **Scanner** tab shows rejected evaluations with their reason, which is the
most useful screen on a quiet day. "Required market data unavailable: rvol" is a
sentence you should expect to see in your first two weeks, and it is the system
working.

---

## Step 11 — Read the record

**History.** The range summary, then a day-by-day table, then trade by trade,
then one trade down to what each fill cost.

Three columns that never collapse into one: **gross**, **charges**, **net**. A
day that made ₹900 before costs and ₹340 after is not a ₹900 day, and the strategy
assessment is judged on net for the same reason.

The **reconciliation** column says one of four things:

| Status | Meaning | What to do |
|---|---|---|
| **Estimated charges** | The cost figure is this system's model of the rate card | Nothing. This is the normal state of a paper day |
| **Broker data pending** | Live trades were taken; the broker's figures have not been fetched | Press **Fetch** on that row |
| **Matched** | The broker agrees within ₹1 | Nothing |
| **Mismatch** | The broker's realised P&L differs by more than ₹1 | Investigate before trusting the day |

"Estimated charges" is not a fault. Upstox reports charges aggregated over a
date range and never per trade, so a per-trade cost here is a local estimate
permanently.

Broker figures are never written over yours. They land in a separate
append-only table, and when they disagree the screen shows both.

**Export** gives you CSV (trades) or a three-sheet Excel file (summary, days,
trades) for whatever range is on screen.

---

## Step 12 — The daily routine

**Before 09:15**
1. Upstox token is fresh — the console says **Token Active**, or press **Renew
   Access (Web Login)**.
2. Dashboard shows everything healthy.
3. Start the scanner.

**During**
Leave it alone. That is the point of it.

**After 15:30**
1. Stop the scanner.
2. History → check the day.
3. If any live trades were taken, press **Fetch** on the day (the 18:00 job does
   this too, and skips itself on a paper-only day).

**Weekly**
Strategies → Details on each. The verdict panel will say
**Not enough evidence** for a long time. That is not a bug; it needs at least 30
resolved trades on each of out-of-sample backtest and paper-forward before it
will say anything at all, and even then its strongest verdict is *promising, not
proven*. There is no code path in it that calls a strategy profitable.

---

## Step 13 — Back it up

The database holds every signal, order, fill, position and settings revision.
Losing it loses the evidence the whole exercise is for.

```sh
./scripts/backup-postgres.sh        # Linux/macOS/NAS
.\scripts\backup-postgres.ps1       # Windows
```

Put it on a schedule — Synology Task Scheduler, weekdays after 18:00 — and copy
the output somewhere that is not the NAS.

---

## Step 14 — Troubleshooting

**No signals at all.**
Scanner & Universe → Scanner, look at the rejection reasons. In order of
likelihood: RVOL baseline not yet accumulated (Step 6), market data not
connected (Step 7), the universe is empty because the scanner has not run
pre-open, or the day already halted on its profit or loss limit — the Risk
screen says so.

**Signals but no orders.**
Risk → today's budget. Four trades a day, and the daily risk budget may allow
fewer. The Effective limits panel in Settings names the binding control.

**"Required market data unavailable: …"**
Working as intended. That input is missing or stale, and the strategy that needs
it is refusing rather than scoring a guess as confidence.

**Upstox stops working, usually first thing in the morning.**
The access token expired. The Upstox console will say **No Active Token**.
Renew it (Step 7).

**The API will not start.**
`docker compose logs api | tail -50`. The settings validator refuses with a
specific message. The most common three: a placeholder `JWT_SECRET`,
`AUTO_CREATE_SCHEMA=true` with `APP_ENV=production`, and
`LIVE_TRADING_ENABLED=true`, which is refused always.

**Migrations out of date after a `git pull`.**

```sh
docker compose build api web
docker compose up -d
docker compose exec api alembic upgrade head
```

---

## What this has *not* set up

Live trading. Everything above is simulated.

Before that is even a conversation you need a paper record worth reading: at
least 30 resolved trades per strategy, a square-off time set, and reconciliation
you have actually looked at. Then read `GOING_LIVE_WITH_UPSTOX.md`, which covers
the static IP, the broker selection, the approval mode, the readiness gates, the
activation window, and the dry run that has to accept and cancel one real order
before the lock comes off.

The lock comes off in its own commit, with the evidence in the message. Not as
part of a setup.
