"""Scanner process entry point.

Handles continuous market-data streaming, candle aggregation, and paper strategy scanning.
"""

import asyncio
import contextlib
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
from app.services.data_quality import MarketDataQualityService
from app.services.firstock.market_data import FirstockMarketDataService
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.trading_calendar import MARKET_TIMEZONE, TradingCalendar
from app.services.universe import refresh_universe
from app.services.upstox_backfill import backfill_daily_history, backfill_today_candles
from app.services.upstox_instruments import InstrumentRefreshError, refresh_is_due, refresh_upstox_instruments
from app.services.upstox_market_data import UpstoxMarketDataService
from app.services.upstox_oauth import UpstoxOAuthError, load_access_token
from app.services.worker_supervision import WORKER_STATE_KEY, RestartBackoff, completed_task_detail


async def _publish_worker_state(redis: Redis, status: str, detail: str, restart_count: int) -> None:
    await redis.hset(
        WORKER_STATE_KEY,
        mapping={
            "status": status,
            "detail": detail,
            "restart_count": str(restart_count),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger("scanner")
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    market_data_task: asyncio.Task[None] | None = None
    aggregation: CandleAggregationService | None = None
    active_broker: str | None = None
    active_token: str | None = None
    market_data_started_at: float | None = None
    market_data_catchup_done = False
    active_persistence: MarketCalculationPersistenceService | None = None
    active_benchmark: str | None = None
    last_heartbeat = 0.0
    last_heartbeat_log = 0.0
    last_instrument_check = 0.0
    last_universe_refresh: object = None
    market_data_backoff = RestartBackoff()
    loop_backoff = RestartBackoff(maximum_seconds=30)
    calendar = TradingCalendar.from_settings(settings)
    logger.info("scanner.worker_started", mode=settings.application_mode, live_trading_enabled=False)
    await _publish_worker_state(redis, "RUNNING", "Scanner worker supervisor started", 0)
    try:
        while True:
            try:
                now = time.monotonic()
                if now - last_heartbeat >= 30:
                    await redis.set("scanner:heartbeat", datetime.now(UTC).isoformat(), ex=90)
                    last_heartbeat = now

                requested_state = await redis.get("scanner:control_state") or "STOPPED"
                broker_controls = await load_broker_controls(redis)
                selected_broker = broker_controls.active_broker

                if market_data_task and market_data_task.done():
                    detail = completed_task_detail(market_data_task)
                    if requested_state == "RUNNING":
                        delay = market_data_backoff.record_failure(now)
                        logger.warning(
                            "scanner.market_data_task_ended",
                            provider=active_broker,
                            detail=detail,
                            restart_in_seconds=delay,
                            restart_count=market_data_backoff.failures,
                        )
                        await _publish_worker_state(
                            redis,
                            "DEGRADED",
                            f"{detail}; retrying in {delay:g} seconds",
                            market_data_backoff.failures,
                        )
                    else:
                        market_data_backoff.reset()
                    market_data_task = None
                    aggregation = None
                    active_broker = None
                    market_data_started_at = None
                elif (
                    market_data_task
                    and market_data_started_at is not None
                    and now - market_data_started_at >= 60
                    and market_data_backoff.failures
                ):
                    market_data_backoff.reset()
                    await _publish_worker_state(redis, "RUNNING", "Market-data task is stable", 0)

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
                        market_data_started_at = None
                        market_data_backoff.reset()

                    if now - last_instrument_check >= 300:
                        last_instrument_check = now
                        try:
                            if await refresh_is_due(settings):
                                refreshed = await refresh_upstox_instruments(settings)
                                logger.info("scanner.upstox_instruments_refreshed", missing_keys=refreshed.missing_keys)
                        except InstrumentRefreshError as exc:
                            logger.warning("scanner.upstox_instruments_refresh_failed", error=str(exc))

                if requested_state == "RUNNING" and selected_broker != active_broker and market_data_task:
                    market_data_task.cancel()
                    await asyncio.gather(market_data_task, return_exceptions=True)
                    market_data_task = None
                    aggregation = None
                    active_broker = None
                    market_data_started_at = None
                    market_data_backoff.reset()

                if (
                    requested_state == "RUNNING"
                    and selected_broker != "NONE"
                    and market_data_task is None
                    and market_data_backoff.ready(now)
                ):
                    benchmark = (
                        settings.upstox_nifty_benchmark_key
                        if selected_broker == "UPSTOX"
                        else settings.nifty_benchmark_token
                    )
                    data_quality = MarketDataQualityService(settings, redis, calendar)
                    scanner = PaperScannerOrchestrator(settings, redis, benchmark)
                    persistence = MarketCalculationPersistenceService(
                        settings,
                        redis,
                        scanner.on_completed_candle,
                        benchmark,
                        data_quality=data_quality,
                    )
                    aggregation = CandleAggregationService(
                        settings.candle_timeframe_seconds,
                        persistence.persist_completed,
                        calendar=calendar,
                        data_quality=data_quality,
                    )

                    if selected_broker == "UPSTOX":
                        # Backfill today's intraday history so Opening Range & Indicators are ready immediately
                        try:
                            logger.info("scanner.starting_intraday_backfill", provider="UPSTOX")
                            await backfill_today_candles(
                                settings,
                                persistence,
                                access_token=current_token,
                                benchmark_key=benchmark,
                                calendar=calendar,
                            )
                        except Exception as exc:
                            logger.warning("scanner.upstox_backfill_failed", error=str(exc))

                        try:
                            await backfill_daily_history(
                                settings, persistence, access_token=current_token, benchmark_key=benchmark
                            )
                        except Exception as exc:
                            logger.warning("scanner.upstox_daily_backfill_failed", error=str(exc))

                        service = UpstoxMarketDataService(settings, redis, aggregation.consume, current_token)
                        active_token = current_token
                    else:
                        service = FirstockMarketDataService(settings, redis, aggregation.consume)
                        logger.warning(
                            "scanner.no_intraday_backfill",
                            provider=selected_broker,
                            detail=(
                                "This provider has no historical intraday backfill; the opening "
                                "range and session indicators only become trustworthy once the live "
                                "feed has covered the whole session. Until then the data-quality "
                                "gate suppresses signals for instruments with missing candle buckets."
                            ),
                        )

                    market_data_task = asyncio.create_task(
                        service.run_forever(), name=f"{selected_broker.lower()}-feed"
                    )
                    market_data_started_at = now
                    market_data_catchup_done = selected_broker != "UPSTOX"
                    active_persistence = persistence
                    active_benchmark = benchmark
                    active_broker = selected_broker
                    logger.info("scanner.market_data_started", provider=selected_broker, benchmark=benchmark)
                elif requested_state != "RUNNING" and market_data_task and not market_data_task.done():
                    market_data_task.cancel()
                    await asyncio.gather(market_data_task, return_exceptions=True)
                    market_data_task = None
                    aggregation = None
                    active_broker = None
                    active_token = None
                    market_data_started_at = None
                    market_data_backoff.reset()
                    logger.info("scanner.market_data_stopped")
                elif requested_state == "RUNNING" and selected_broker == "NONE":
                    active_broker = None
                    active_token = None

                if aggregation is not None:
                    await aggregation.flush_expired()

                if (
                    not market_data_catchup_done
                    and market_data_task is not None
                    and not market_data_task.done()
                    and market_data_started_at is not None
                    and now - market_data_started_at >= 150
                    and active_persistence is not None
                ):
                    # A mid-session (re)start leaves a 1-2 candle seam: the intraday
                    # backfill lags the last closed minute, and the live feed only
                    # captures buckets after the websocket connects. Once the feed has
                    # been stable for a bit, re-run the (idempotent) intraday backfill
                    # to fill that gap before the data-quality gate locks the session.
                    market_data_catchup_done = True
                    try:
                        filled = await backfill_today_candles(
                            settings,
                            active_persistence,
                            access_token=current_token,
                            benchmark_key=active_benchmark,
                            calendar=calendar,
                        )
                        logger.info("scanner.market_data_catchup_backfill", candles=filled)
                    except Exception as exc:
                        logger.warning("scanner.market_data_catchup_failed", error=str(exc))

                if settings.universe_enabled and requested_state == "RUNNING" and selected_broker == "UPSTOX":
                    market_now = datetime.now(UTC)
                    market_status = calendar.status_at(market_now)
                    local_hhmm = market_now.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
                    today = market_now.astimezone(MARKET_TIMEZONE).date()
                    if (
                        market_status.trading_day
                        and local_hhmm >= settings.universe_refresh_time
                        and last_universe_refresh != today
                    ):
                        try:
                            summary = await refresh_universe(settings, today)
                            last_universe_refresh = today
                            logger.info("scanner.universe_refreshed", **summary)
                        except Exception as exc:
                            logger.warning("scanner.universe_refresh_failed", error=str(exc))

                if now - last_heartbeat_log >= 30:
                    logger.info("scanner.worker_heartbeat", status=requested_state, provider=active_broker)
                    last_heartbeat_log = now

                if loop_backoff.failures and market_data_backoff.failures == 0:
                    # A prior iteration failed and published DEGRADED; the loop has since
                    # recovered and market-data is healthy, so clear the stale state.
                    with contextlib.suppress(Exception):
                        await _publish_worker_state(
                            redis, "RUNNING", "Scanner worker recovered after a transient error", 0
                        )
                loop_backoff.reset()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = loop_backoff.record_failure()
                logger.exception("scanner.worker_iteration_failed", error=str(exc), retry_in_seconds=delay)
                with contextlib.suppress(Exception):
                    await _publish_worker_state(
                        redis,
                        "DEGRADED",
                        f"Worker iteration failed: {type(exc).__name__}; retrying in {delay:g} seconds",
                        loop_backoff.failures,
                    )
                await asyncio.sleep(delay)
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
