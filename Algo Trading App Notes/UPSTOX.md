# Upstox paper market-data setup

Upstox is the default market-data connector for the paper scanner. This integration
uses the official Upstox V3 market-data feed through its Python SDK. It contains no
portfolio, order, or trade-submission API call.

## 1. Create the developer application

In the Upstox Developer Portal, create an application and record its API key, API
secret, and registered redirect URI. Keep those values private. The connector accepts
a short-lived server-side access token and can also complete the official browser-
based authorization-code flow. The API key, secret, redirect URI, and token-
encryption key are never sent to the browser.

## 2. Obtain an access token

Use Upstox's supported developer-token/OAuth flow to obtain an access token. Put it
only in the server `.env` for a manual fallback. Upstox tokens expire at 03:30 IST;
the application cannot silently extend that broker session. An ADMIN can start a new
authorization-code exchange at `POST /api/v1/market-data/upstox/authorize`. The
callback validates a one-time Redis state value, exchanges the code server-side, and
encrypts the resulting token at rest using `UPSTOX_TOKEN_ENCRYPTION_KEY`.

## 3. Choose confirmed instrument keys

Download or query the current Upstox instrument master and use its `instrument_key`
values. Do not substitute exchange tokens: Upstox documents that exchange tokens can
be reused. Examples below are illustrative; verify every key in the current master.

```text
UPSTOX_ACCESS_TOKEN=replace-with-current-token
UPSTOX_API_KEY=your-developer-app-api-key
UPSTOX_API_SECRET=your-developer-app-api-secret
UPSTOX_REDIRECT_URI=https://your-domain.example/upstox/callback
UPSTOX_SUBSCRIPTIONS=NSE_INDEX|Nifty 50,NSE_EQ|INE002A01018
UPSTOX_NIFTY_BENCHMARK_KEY=NSE_INDEX|Nifty 50
```

`UPSTOX_SUBSCRIPTIONS` is a comma-separated list. Include the same benchmark key in
the subscription list. Never commit `.env` or send its values in chat.

## Instrument-master refresh

The scanner worker downloads Upstox's published NSE JSON instrument master at startup
and then at `UPSTOX_INSTRUMENT_REFRESH_HOURS` (24 by default). It stores only refresh
metadata and configured-instrument records, not the complete master. Missing keys are
logged for action; the feed does not invent replacement keys.

## 4. Build and select the connector

From the project folder:

```powershell
docker compose up --build -d
docker compose ps
```

Sign in as an administrator, open **Settings**, and select **Use Upstox PAPER** under
**Market-data connectors**. The scanner worker notices a connector change within a
few seconds; stop and start the scanner after the dashboard shows the Upstox feed as
configured.

Only one feed can be selected at a time. Selecting Firstock disables Upstox, and
**Disable both** stops connector ingestion. These controls cannot enable live
trading and do not submit broker orders.

## Troubleshooting

- `NOT_CONFIGURED`: set both `UPSTOX_ACCESS_TOKEN` and `UPSTOX_SUBSCRIPTIONS`, then
  rebuild the API and scanner-worker containers.
- `DEGRADED`: refresh the expired access token, verify the instrument keys from the
  current master, and inspect `docker compose logs scanner-worker --tail 100`.
- No signals: a live feed alone is not a signal. The scanner requires completed
  candles and all paper-strategy/risk controls to qualify a decision.

## References

- [Upstox authentication](https://upstox.com/developer/api-documentation/authentication/)
- [Upstox access-token lifecycle](https://upstox.com/developer/api-documentation/get-token)
- [Upstox V3 Market Data Feed](https://upstox.com/developer/api-documentation/v3/get-market-data-feed/)
- [Upstox instruments](https://upstox.com/developer/api-documentation/instruments/)
