# Firstock Integration (retained alternative feed)

## Phase 3 implementation

The backend uses Firstock Developer API V1 login and WebSocket V2 market feed only.
It is retained as a disabled-by-default alternative to the Upstox paper feed. No
order-placement method exists in this project.

- Login: `POST https://api.firstock.in/V1/login`; the password is SHA-256 hashed before sending.
- Market feed: `wss://socket.firstock.in/V2/ws` with `userId`, the returned session token (`jKey`), and `source=developer-api` query parameters.
- Subscribe: `{"action":"subscribe","tokens":"NSE:token|NSE:token"}`.
- The WebSocket client uses ping/pong, automatic subscription recovery, exponential retry from 2 to 60 seconds, and Redis state reporting.

## Local setup

Set these values only in the server `.env`; never put them in a `NEXT_PUBLIC_*` variable or paste them into chat. Then choose **Use Firstock feed** in the protected Settings page if you intentionally want to use it instead of Upstox:

```text
FIRSTOCK_USER_ID=
FIRSTOCK_PASSWORD=
FIRSTOCK_VENDOR_CODE=
FIRSTOCK_API_KEY=
FIRSTOCK_TOTP_SECRET=
FIRSTOCK_SUBSCRIPTIONS=NSE:your_token|NSE:another_token
```

`FIRSTOCK_TOTP_SECRET` is optional only when TOTP is not enabled. Confirm every exchange/token pair from Firstock before adding it; the worker does not guess tokens. Rebuild the containers after changing `.env`, then use the authenticated `POST /api/v1/broker/firstock/test` endpoint in FastAPI docs or dashboard status to verify authentication. A successful test authenticates only; it never opens an order channel or submits an order.

## Known boundary

The instrument master/search and historical-candle REST contracts require a final account-level verification before they are invoked. Their adapters remain intentionally isolated rather than guessing request parameters from older documentation.
