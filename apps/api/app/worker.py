"""Scanner process entry point.

Handles continuous market-data streaming, candle aggregation, and paper strategy scanning.
"""

import asyncio
import time
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.broker_controls import load_broker_controls
from app.services.candle_aggregation import (
    CandleAggregationService,
    MarketCalculationPersistenceService,
)
from app.services.firstock.market_data import FirstockMarketDataService
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.upstox_backfill import backfill_today_candles
from app.services.upstox_instruments import InstrumentRefreshError, refresh_is_due, refresh_upstox_instruments
from app.services.upstox_market_data import UpstoxMarketDataService
from app.services.upstox_oauth import UpstoxOAuthError, load_access_token


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger("scanner")
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    market_data_task: asyncio.Task[None] | None = None
    aggregation: CandleAggregationService | None = None
    active_broker: str | None = None
    active_token: str | None = None
    last_heartbeat = 0.0
    last_instrument_check = 0.0
    logger.info("scanner.worker_started", mode=settings.application_mode, live_trading_enabled=False)
    try:
        while True:
            if time.monotonic() - last_heartbeat >= 30:
                await redis.set("scanner:heartbeat", datetime.now(UTC).isoformat(), ex=90)
                last_heartbeat = time.monotonic()

            requested_state = await redis.get("scanner:control_state") or "STOPPED"
            broker_controls = await load_broker_controls(redis)
            selected_broker = broker_controls.active_broker

            # Check for Upstox token changes or renewal
            current_token: str | None = None
            if selected_broker == "UPSTOX":
                try:
                    current_token = await load_access_token(settings)
                except UpstoxOAuthError:
                    current_token = None

                if active_token != current_token and market_data_task:
                    logger.info("scanner.token_updated_reconnecting", provider=selected_broker)
                    market_data_task.cancel()
                    await asyncio.gather(market_data_task, return_exceptions=True)
                    market_data_task = None
                    aggregation = None
                    active_broker = None

                if time.monotonic() - last_instrument_check >= 300:
                    last_instrument_check = time.monotonic()
                    try:
                        if await refresh_is_due(settings):
                            refreshed = await refresh_upstox_instruments(settings)
                            logger.info("scanner.upstox_instruments_refreshed", missing_keys=refreshed.missing_keys)
                    except InstrumentRefreshError:
                        logger.warning("scanner.upstox_instruments_refresh_failed")

            if requested_state == "RUNNING" and selected_broker != active_broker and market_data_task:
                market_data_task.cancel()
                await asyncio.gather(market_data_task, return_exceptions=True)
                market_data_task = None
                aggregation = None
                active_broker = None

            if (
                requested_state == "RUNNING"
                and selected_broker != "NONE"
                and (market_data_task is None or market_data_task.done())
            ):
                benchmark = (
                    settings.upstox_nifty_benchmark_key
                    if selected_broker == "UPSTOX"
                    else settings.nifty_benchmark_token
                )
                scanner = PaperScannerOrchestrator(settings, redis, benchmark)
                persistence = MarketCalculationPersistenceService(
                    settings, redis, scanner.on_completed_candle, benchmark
                )
                aggregation = CandleAggregationService(settings.candle_timeframe_seconds, persistence.persist_completed)

                if selected_broker == "UPSTOX":
                    # Backfill today's intraday history so Opening Range & Indicators are ready immediately
                    try:
                        logger.info("scanner.starting_intraday_backfill", provider="UPSTOX")
                        await backfill_today_candles(
                            settings, persistence, access_token=current_token, benchmark_key=benchmark
                        )
                    except Exception as exc:
                        logger.warning("scanner.upstox_backfill_failed", error=str(exc))

                    service = UpstoxMarketDataService(settings, redis, aggregation.consume, current_token)
                    active_token = current_token
                else:
                    service = FirstockMarketDataService(settings, redis, aggregation.consume)

                market_data_task = asyncio.create_task(service.run_forever())
                active_broker = selected_broker
                logger.info("scanner.market_data_started", provider=selected_broker)
            elif requested_state != "RUNNING" and market_data_task and not market_data_task.done():
                market_data_task.cancel()
                await asyncio.gather(market_data_task, return_exceptions=True)
                market_data_task = None
                aggregation = None
                active_broker = None
                active_token = None
                logger.info("scanner.market_data_stopped")
            elif requested_state == "RUNNING" and selected_broker == "NONE":
                active_broker = None
                active_token = None

            if aggregation is not None:
                await aggregation.flush_expired()

            if int(time.monotonic()) % 30 == 0:
                logger.info("scanner.worker_heartbeat", status=requested_state)

            await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.info("scanner.worker_stopped")
        raise
    finally:
        if market_data_task and not market_data_task.done():
            market_data_task.cancel()
            await asyncio.gather(market_data_task, return_exceptions=True)
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
