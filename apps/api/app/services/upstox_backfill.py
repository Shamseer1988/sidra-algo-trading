"""Intraday historical candle backfill for Upstox market data.

Fetches today's completed 1-minute candles from market open (09:15 IST) up to current time
to establish the 15-minute Opening Range (ORB) and session indicators immediately.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.core.config import Settings
from app.services.candle_aggregation import MarketCalculationPersistenceService
from app.services.market_calculations import CompletedCandle, is_regular_market_timestamp
from app.services.trading_calendar import DEFAULT_TRADING_CALENDAR, TradingCalendar
from app.services.upstox_market_data import configured_subscriptions

logger = structlog.get_logger("upstox.backfill")
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/intraday/{instrument_key}/1minute"
UPSTOX_DAILY_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
DAILY_TIMEFRAME_SECONDS = 86_400


async def backfill_today_candles(
    settings: Settings,
    persistence: MarketCalculationPersistenceService,
    access_token: str | None = None,
    benchmark_key: str | None = None,
    calendar: TradingCalendar | None = None,
) -> int:
    """Fetch and persist today's historical 1-minute candles for all configured instruments."""
    token = access_token or settings.upstox_access_token
    if not token:
        logger.warning("upstox.backfill_skipped_no_token")
        return 0

    benchmark = benchmark_key or settings.upstox_nifty_benchmark_key
    instruments = [benchmark] + [k for k in configured_subscriptions(settings) if k != benchmark]
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    total_backfilled = 0
    today_date = datetime.now(MARKET_TIMEZONE).date()

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for instrument_key in instruments:
            url = UPSTOX_HISTORICAL_URL.format(instrument_key=instrument_key)
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(
                        "upstox.backfill_failed_status", instrument=instrument_key, status=response.status_code
                    )
                    continue

                data = response.json()
                raw_candles = data.get("data", {}).get("candles", [])
                if not raw_candles:
                    continue

                # Upstox returns candles in reverse chronological order: [latest, ..., earliest]
                # We need chronological order: earliest (09:15) -> latest
                sorted_candles = list(reversed(raw_candles))
                instrument_count = 0

                for item in sorted_candles:
                    # Item format: [timestamp_str, open, high, low, close, volume, open_interest]
                    if len(item) < 6:
                        continue
                    ts_str, open_p, high_p, low_p, close_p, vol = item[0], item[1], item[2], item[3], item[4], item[5]
                    try:
                        opened_at = datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue

                    # Filter for today's session within regular market hours
                    if opened_at.astimezone(MARKET_TIMEZONE).date() != today_date:
                        continue
                    if not is_regular_market_timestamp(opened_at, calendar or DEFAULT_TRADING_CALENDAR):
                        continue

                    candle = CompletedCandle(
                        instrument_token=instrument_key,
                        timeframe_seconds=settings.candle_timeframe_seconds,
                        opened_at=opened_at,
                        closed_at=opened_at + timedelta(seconds=settings.candle_timeframe_seconds),
                        open=Decimal(str(open_p)),
                        high=Decimal(str(high_p)),
                        low=Decimal(str(low_p)),
                        close=Decimal(str(close_p)),
                        volume=int(vol or 0),
                        tick_count=1,
                    )
                    # Backfill rebuilds indicators and quality history only. Strategy
                    # evaluation is reserved for candles completed by the live feed.
                    await persistence.persist_completed(candle, notify_snapshot=False)
                    instrument_count += 1

                total_backfilled += instrument_count
                logger.info("upstox.instrument_backfilled", instrument=instrument_key, candles_count=instrument_count)

            except Exception as exc:
                logger.warning("upstox.backfill_instrument_error", instrument=instrument_key, error=str(exc))

    logger.info("upstox.backfill_completed", total_candles=total_backfilled)
    return total_backfilled


async def backfill_daily_history(
    settings: Settings,
    persistence: MarketCalculationPersistenceService,
    access_token: str | None = None,
    benchmark_key: str | None = None,
) -> int:
    """Fetch recent completed daily candles so prior-day levels, gaps and daily ATR are available.

    Stored in ``market_candles`` with ``timeframe_seconds = 86400``; the live 1-minute pipeline
    never reads that timeframe, so the two histories stay isolated.
    """
    token = access_token or settings.upstox_access_token
    if not token:
        logger.warning("upstox.daily_backfill_skipped_no_token")
        return 0

    benchmark = benchmark_key or settings.upstox_nifty_benchmark_key
    instruments = [benchmark] + [k for k in configured_subscriptions(settings) if k != benchmark]
    today_ist = datetime.now(MARKET_TIMEZONE).date()
    to_date = today_ist - timedelta(days=1)
    from_date = today_ist - timedelta(days=max(settings.daily_history_sessions, 5) * 2 + 10)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    total = 0
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for instrument_key in instruments:
            url = UPSTOX_DAILY_HISTORICAL_URL.format(
                instrument_key=instrument_key,
                to_date=to_date.isoformat(),
                from_date=from_date.isoformat(),
            )
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(
                        "upstox.daily_backfill_failed_status", instrument=instrument_key, status=response.status_code
                    )
                    continue
                raw_candles = response.json().get("data", {}).get("candles", [])
                sorted_candles = sorted(raw_candles, key=lambda item: item[0])[-settings.daily_history_sessions :]
                stored = 0
                for item in sorted_candles:
                    if len(item) < 6:
                        continue
                    try:
                        session_date = date.fromisoformat(str(item[0])[:10])
                    except ValueError:
                        continue
                    opened_at = datetime.combine(session_date, time(9, 15), tzinfo=MARKET_TIMEZONE)
                    candle = CompletedCandle(
                        instrument_token=instrument_key,
                        timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
                        opened_at=opened_at,
                        closed_at=datetime.combine(session_date, time(15, 30), tzinfo=MARKET_TIMEZONE),
                        open=Decimal(str(item[1])),
                        high=Decimal(str(item[2])),
                        low=Decimal(str(item[3])),
                        close=Decimal(str(item[4])),
                        volume=int(item[5] or 0),
                        tick_count=1,
                    )
                    if await persistence.persist_daily_candle(candle):
                        stored += 1
                total += stored
                logger.info("upstox.daily_instrument_backfilled", instrument=instrument_key, sessions=stored)
            except Exception as exc:
                logger.warning("upstox.daily_backfill_instrument_error", instrument=instrument_key, error=str(exc))

    logger.info("upstox.daily_backfill_completed", total_sessions=total)
    return total
