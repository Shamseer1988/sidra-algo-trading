# Strategy

## Market-calculation foundation

Phase 4 converts Firstock ticks into one-minute completed OHLCV candles and stores them
in PostgreSQL. No partial candle is persisted or passed to future strategy logic.

- Session calculations use `Asia/Kolkata` and the fail-closed NSE Trading Calendar.
  The regular-session opening range begins at 09:15, ends at 15:30, and defaults
  to 15 minutes. Weekends, confirmed exchange holidays, pre-/post-market ticks,
  and dates outside confirmed calendar coverage are rejected before persistence.
- VWAP is session-anchored and volume-weighted using each completed candle's typical
  price `(high + low + close) / 3`.
- EMA(9), EMA(21), current/average/relative volume, opening-range high/low, stock-vs-
  NIFTY relative strength, and a NIFTY bullish/bearish/neutral regime are saved as an
  indicator snapshot for each completed candle.
- Relative strength and NIFTY regime require `NSE:26000` to be included in
  `FIRSTOCK_SUBSCRIPTIONS`. Missing history reports `null` / `INSUFFICIENT_DATA`; it
  never fabricates values.
- Relative strength compares only stock and NIFTY candles with the same completed
  timestamp; a delayed benchmark candle cannot contribute future information.

Calculation settings live in `.env`: `CANDLE_TIMEFRAME_SECONDS`,
`OPENING_RANGE_MINUTES`, `EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD`, and
`VOLUME_LOOKBACK_CANDLES`.

The bundled 2026 cash-market holidays are based on NSE circular
`NSE/CMTR/71775` plus the announced 15 January closure. Operators must confirm
new calendar years with `NSE_CALENDAR_CONFIRMED_YEARS`; additional closures and
special sessions can be supplied through `NSE_HOLIDAY_OVERRIDES` and
`NSE_SPECIAL_SESSIONS`. An unconfirmed year is closed, never assumed tradable.

## Market-data quality gate

Every instrument publishes a Redis-backed quality snapshot containing expected and
received bars, missing buckets, tick freshness, transport latency, duplicate ticks,
and out-of-order ticks. The states are `GOOD`, `DEGRADED`, `STALE`, and `INVALID`.
The scanner refuses to evaluate a signal when the snapshot is missing, `STALE`, or
`INVALID`. Current snapshots are available through the authenticated
`GET /api/v1/scanner/data-quality` endpoint.
Historical startup backfill rebuilds candle, indicator, and quality state but never
invokes signal evaluation; only a candle completed by the live feed can do that.

## Paper strategy: opening-range breakout and retest

Phase 5 implements `orb-retest-v1`, a conservative, deterministic paper strategy:

1. After the opening range completes, a close beyond its high/low enters a LONG/SHORT
   breakout state.
2. A later completed candle must retest that level within the configured tolerance and
   close back through it in the breakout direction.
3. The confirmation score has five 20-point components: breakout/retest, VWAP,
   fast/slow EMA alignment, relative volume, and stock-relative-strength plus matching
   NIFTY regime. The default threshold is 90/100.
4. Entry is the confirmation candle close. Stop is the more conservative retest/range
   level; target uses the configured minimum reward:risk. Quantity is derived only from
   account capital and paper risk-per-trade.
5. A confirmation must close within the configured trading window, exceed the minimum
   fast/slow EMA spread, and fit under the remaining configured daily paper-risk limit.

Scanner state is Redis-backed per instrument/session. A PostgreSQL uniqueness key
prevents duplicate same-side daily signals across reconnects. The scanner also observes
paper-tracking enablement, Emergency Stop, daily maximum-signal and risk limits, and
the configured trade window.

Confirmed signals are stored in `paper_signals` and sent to the dedicated Telegram bot
with Approve/Reject buttons. Those buttons record an intent only. They neither enable
live mode nor create a broker order.
