# Telegram

The planned integration is outbound-only (`sendMessage`/`sendPhoto`). It will not run `getUpdates` or another polling loop, so it will not interfere with the existing Stock Journal bot. Redis will enforce alert idempotency and per-symbol cooldowns.
