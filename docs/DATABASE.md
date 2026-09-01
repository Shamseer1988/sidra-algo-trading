# Database

PostgreSQL is required in all non-test environments. Phase 2 adds `application_settings` for non-secret, audit-backed paper-trading controls. Alembic owns schema changes; `AUTO_CREATE_SCHEMA=true` exists only for an empty local-development bootstrap and must be disabled for deployed environments.

Phase 4 adds the time-series tables below. All timestamps are UTC; `session_date` is
derived in `Asia/Kolkata` for correct market-session aggregation.

- `market_candles` stores immutable, completed OHLCV candles. Its uniqueness key is
  instrument token + timeframe + candle open timestamp, preventing duplicate replay
  or reconnect writes.
- `market_indicator_snapshots` stores the derived values attached to that completed
  candle: VWAP, EMAs, volume metrics, opening range, relative strength, and NIFTY
  regime. It has the same one-snapshot-per-candle guarantee.

Phase 5 adds `paper_signals`, which stores a deduplicated paper-scanner decision,
entry/stop/target, calculated size, score breakdown, source indicator snapshot, and
alert delivery status. It is explicitly not an order or position table.

Market-regime history and paper-trade outcome events are future Phase 8 tables.
Partitioning will be introduced only after access patterns and retention requirements
are measured.
