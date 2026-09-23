# Going live with Upstox — what you have to do

Everything in this list is something only you can do. None of it is code, and
none of it can be done from inside the application. The software refuses to send
a live order until all of it is true, which is deliberate: each refusal below is
a gate that has to be satisfied by a person, not by a configuration flag.

Read section 0 first. If the answer there is no, nothing else in this document
matters yet.

---

## 0. The one that gates everything: is your Upstox app allowed to place orders?

Your Upstox app currently streams market data. That is a different permission
from placing an order, and an app set up for data will return an error on the
first live placement — not at startup, not in the readiness report, but at the
moment an order is sent.

**What to check.** Sign in at <https://account.upstox.com/developer/apps> and
open the app whose API key this system uses. Upstox now distinguishes an **Algo
Trading App** from an ordinary one; order placement through the API requires the
Algo Trading type.

**If it is not an Algo Trading App**, you need one. Creating or converting it is
done in that same screen. Expect Upstox to ask you to accept the API terms and
to confirm you understand the SEBI algo-trading rules.

**How you will know it worked**: section 5's dry run places one real order for
one share and cancels it. Nothing before that proves the permission exists.

---

## 1. Static IP — yes, this is required

Since 1 April 2026 the exchanges require every API order to originate from a
static IP address registered with your broker in advance. Orders from any other
address are rejected outright. This is not optional and it is not something the
code can work around.

You get **two** addresses: a Primary and a Secondary (for failover). Both are
configured on the app itself in My Apps.

**Three things that will bite you:**

1. **Your NAS almost certainly does not have a static public IP.** A home or
   office broadband connection from Ooredoo/Vodafone/Jio gets a dynamic address
   that changes. Registering today's address means orders start being rejected
   the next time your ISP renews it.

2. **You can only change the registered IP once per calendar week.** So if the
   address does drift, you cannot simply re-register it each morning. One wrong
   week and you are out of the market until the next one.

3. **Changing the IP invalidates your existing access token.** You will have to
   re-authorise (section 3) immediately afterwards.

**Your options, in the order I would try them:**

| Option | What it costs | Why it might not suit you |
|---|---|---|
| Ask your ISP for a static IP on the existing line | Usually a small monthly fee in Qatar and India both | Some consumer plans simply will not sell one; you may need a business plan |
| Put the API container behind a small cloud VM with a fixed IP and tunnel to it | ~$5/month for the VM | One more moving part to keep alive; the tunnel becoming the single point of failure |
| Run the whole API on a cloud VM instead of the NAS | VM cost, plus moving Postgres | Your data and Home Assistant integration stay on the NAS, so this splits the system |
| A commercial static-IP proxy (QuotaGuard and similar advertise this for Upstox specifically) | Subscription | You are trusting a third party with traffic that carries your access token |

**My recommendation**: ask your ISP first. It is the only option with no extra
failure mode. If they will not, the small cloud VM with a tunnel is the next
best, and you already run Cloudflare Tunnels, so the shape of it is familiar.

**Find your current public IP** from the NAS, so you know what you are
registering:

```
curl -s https://api.ipify.org
```

Run that on the NAS itself, not on your laptop — it is the NAS's address that
has to be registered, because that is where the orders leave from.

---

## 2. SEBI algo registration — you almost certainly do not need it

An algo must be registered with the exchange, and carry an Algo ID, **only if it
places more than ten orders per second**. Your system is configured for two to
three trades per day. You are nowhere near that threshold, so no registration,
no Algo ID, and no waiting on an exchange approval.

If that ever changes, the application already supports it: set
`UPSTOX_ALGO_NAME` in the API environment to the exact name registered in Upstox
My Apps (case-sensitive), and it is sent as the `X-Algo-Name` header on every
call. Leave it unset until you actually have one — sending an unregistered name
is worse than sending none.

---

## 3. Authorise the app for trading, and know when it expires

Upstox access tokens expire **daily**, at 03:30 IST. A token obtained yesterday
will not place an order today.

The application has two ways to handle this:

- **Manual**: an admin presses the authorise button in the web UI, which sends
  you to Upstox, and the callback stores the token encrypted.
- **Automatic**: `UPSTOX_AUTO_AUTH_ENABLED=true` with the mobile number, PIN and
  TOTP secret configured, so it re-authorises itself each morning.

You are already using one of these for market data. The same token covers order
placement — there is no second authorisation — **provided** the app is an Algo
Trading App per section 0.

**After you register or change a static IP, re-authorise immediately.** The IP
change invalidates the current token, and a system that looks configured but
holds a dead token fails at the worst possible moment.

---

## 4. Funding, and a number you should decide before you start

Put **₹10,000** in the Upstox account and nothing more, for now.

The system is configured for a ₹10,000 account with 5× intraday leverage, a
₹2,000 daily profit target and a ₹1,000 daily loss limit. Those limits are
enforced in code and the day stops when either is hit. But the limits protect
the *strategy's* behaviour, not the account — a bug, a stuck order, or an
unresolved submission can cost more than the limit says.

Keep the funded amount to what you are willing to lose entirely while the system
is new. Increase it when you have live evidence, not before.

**Do not connect an account that holds positions or funds you need.** Live
reconciliation blocks trading whenever it finds a position it cannot explain, so
existing holdings in the same account will simply stop the system from ever
arming.

---

## 5. The dry run, before any strategy order

This is the step that proves sections 0 to 3 actually worked. It places **one
real order for one share at a price far from the market**, confirms it appears
in the order book with the identifier we sent, and cancels it. Total risk: the
brokerage on a cancelled order, which is nil.

Do not skip it. It is the only thing that distinguishes "configured" from
"working", and every failure mode above produces the same symptom otherwise — a
rejected order with a message that does not say which of the five causes it was.

I will write this dry-run script and give you the exact command. It does not
exist yet; it is the next thing I build.

---

## 6. Things in the application that you, as admin, still have to switch on

These are in the web UI and the API environment, and they are deliberately
separate from each other so no single change turns live trading on.

| What | Where | Note |
|---|---|---|
| **Live broker = UPSTOX** | Settings → Live broker | Defaults to NONE, which sends nothing. Nothing else in this table matters until this is set. |
| **Approval mode = TELEGRAM_APPROVAL** | Settings → Approval mode | Start here, not AUTOMATIC. Every order asks you on Telegram first and expires in 3 minutes if you do not answer. |
| `APPLICATION_MODE=LIVE` | API environment | Restart required. |
| `LIVE_TRADING_ENABLED=true` | API environment | Restart required. |
| `LIVE_COMPLIANCE_APPROVED=true` | API environment | Your attestation that you have read the SEBI rules. Only set it once you have. |
| `LIVE_STATIC_IP_VERIFIED=true` | API environment | Set this **after** section 1 is genuinely done, not in anticipation. It is a claim about the world, and the system trusts it. |
| **Live reconciliation** | Live page → Reconcile | Must have run and come back clean within the last 15 minutes. |
| **Administrator activation** | Live page → Activate | Expires on its own (8 hours by default) so a system armed this morning is not still armed unattended tonight. |

You can see exactly which of these are outstanding at any moment on the **Live
readiness** page — it lists every gate and says what is blocking each one,
including "no live broker is selected".

---

## 7. What I would not do yet

**Do not go live with the current strategies.** I need to say this plainly,
because everything above is about plumbing and this is about money.

The paper-trading evidence to 23 September shows the four strategies have no
edge: 22 wins in 55 trades, which is 0.400 — exactly what a coin flip with a
1.5 reward-to-risk target produces. Twenty-six of the thirty-three losers never
moved half a unit of risk in your favour before going against you. Costs run
about 0.41 R per trade, so even the best-scoring band loses money net.

None of that is fixed by any setting, any account size, or any broker. It is
fixed by finding an edge, and finding one needs more history than the system
currently holds — roughly eight sessions of 1-minute data per instrument, which
is far too little to test anything on.

So the honest sequence is:

1. Finish the live plumbing (in progress — this document is part of it).
2. Backfill 1-minute history properly, so the strategy sweep has something to
   work with.
3. Find and validate an edge on that history.
4. Then fund the account and trade it small.

Steps 1 and 2 can run in parallel, and I would start the backfill next. Going
live before step 3 means paying real brokerage to re-learn what the paper
journal already told us.

---

## Quick reference: what to send me once you have done it

- Output of `curl -s https://api.ipify.org` run **on the NAS**
- A screenshot of the Upstox My Apps screen showing the app type and the
  registered Primary IP (redact the API key)
- Whether your ISP will sell you a static IP, and which option from section 1
  you chose

With those three things I can tell you exactly what is left.
