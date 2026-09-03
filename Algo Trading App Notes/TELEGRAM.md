# Telegram Control Plane

Use a new, dedicated bot for Intraday Sentinel. Do not reuse the Stock Journal bot.

## Outbound alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the server `.env`. An ADMIN can then call `POST /api/v1/telegram/test` to send a test alert. The service calls only the Bot API methods needed to send messages; it never calls `getUpdates`.

Paper-signal alerts use a Redis `SET NX` cooldown keyed by signal identity
(`TELEGRAM_ALERT_COOLDOWN_SECONDS`, 900 by default). Every sent, failed, or
suppressed scanner notification is retained in `telegram_alerts` for delivery history.

## Inbound controls

Inbound controls use Telegram webhooks—not polling—and therefore require a publicly reachable **HTTPS** URL. Set:

```text
TELEGRAM_WEBHOOK_URL=https://your-domain.example/api/v1/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=generate-a-long-random-value
TELEGRAM_ALLOWED_USER_IDS=123456789
```

The webhook verifies Telegram's `X-Telegram-Bot-Api-Secret-Token` header, requires both the configured chat and an allowed numeric Telegram user ID, and deduplicates `update_id` values.

Supported inbound actions:

- Inline **Approve** / **Reject** callbacks: stored as `TradeApprovalIntent` records only. Release 1 cannot place orders.
- Inline **Emergency Stop** or `/stop`: immediately changes scanner state to `STOPPED`; the worker cancels market-data work within two seconds.

An ADMIN must call `POST /api/v1/telegram/webhook/register` after the HTTPS endpoint and secret are configured. Callback data is intentionally short and all inbound events are audit logged.

## Safety boundary

Telegram approval does not enable live trading or submit broker orders. That future change requires a separate, explicitly authorized Stage 3 implementation with server-side risk validation.
