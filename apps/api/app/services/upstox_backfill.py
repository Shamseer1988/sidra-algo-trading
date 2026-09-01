"""Intraday historical candle backfill for Upstox market data.

Fetches today's completed 1-minute candles from market open (09:15 IST) up to current time
to establish the 15-minute Opening Range (ORB) and session indicators immediately.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.core.config import Settings
from app.services.candle_aggregation import MarketCalculationPersistenceService
from app.services.market_calculations import CompletedCandle, is_regular_market_timestamp
from app.services.upstox_market_data import configured_subscriptions

logger = structlog.get_logger("upstox.backfill")
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/intraday/{instrument_key}/1minute"
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


async def backfill_today_candles(
    settings: Settings,
    persistence: MarketCalculationPersistenceService,
    access_token: str | None = None,
    benchmark_key: str | None = None,
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
                    if not is_regular_market_timestamp(opened_at):
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
                    await persistence.persist_completed(candle)
                    instrument_count += 1

                total_backfilled += instrument_count
                logger.info("upstox.instrument_backfilled", instrument=instrument_key, candles_count=instrument_count)

            except Exception as exc:
                logger.warning("upstox.backfill_instrument_error", instrument=instrument_key, error=str(exc))

    logger.info("upstox.backfill_completed", total_candles=total_backfilled)
    return total_backfilled
