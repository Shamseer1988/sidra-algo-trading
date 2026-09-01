"""Tick aggregation and completed-candle persistence for market calculations."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import MarketCandle, MarketIndicatorSnapshot
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle, indicator_snapshot, is_regular_market_timestamp
from app.services.paper_journal import update_outcomes


@dataclass(frozen=True)
class MarketTick:
    instrument_token: str
    price: Decimal
    cumulative_volume: int | None
    occurred_at: datetime


@dataclass
class _OpenCandle:
    instrument_token: str
    opened_at: datetime
    timeframe_seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    tick_count: int = 0

    def update(self, tick: MarketTick, volume_delta: int) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += max(volume_delta, 0)
        self.tick_count += 1

    def complete(self) -> CompletedCandle:
        return CompletedCandle(
            instrument_token=self.instrument_token,
            timeframe_seconds=self.timeframe_seconds,
            opened_at=self.opened_at,
            closed_at=self.opened_at + timedelta(seconds=self.timeframe_seconds),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            tick_count=self.tick_count,
        )


def _bucket_start(timestamp: datetime, timeframe_seconds: int) -> datetime:
    value = timestamp.astimezone(UTC)
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % timeframe_seconds), tz=UTC)


class CandleAggregationService:
    """Produces only closed candles and tolerates feed reconnects/volume resets."""

    def __init__(self, timeframe_seconds: int, on_completed: Callable[[CompletedCandle], Awaitable[None]]) -> None:
        self._timeframe_seconds = timeframe_seconds
        self._on_completed = on_completed
        self._open: dict[str, _OpenCandle] = {}
        self._last_cumulative_volume: dict[str, int] = {}

    def _volume_delta(self, tick: MarketTick) -> int:
        if tick.cumulative_volume is None:
            return 0
        previous = self._last_cumulative_volume.get(tick.instrument_token)
        self._last_cumulative_volume[tick.instrument_token] = tick.cumulative_volume
        if previous is None:
            return 0
        return tick.cumulative_volume - previous if tick.cumulative_volume >= previous else tick.cumulative_volume

    async def consume(self, tick: MarketTick) -> None:
        if not is_regular_market_timestamp(tick.occurred_at):
            return
        bucket = _bucket_start(tick.occurred_at, self._timeframe_seconds)
        current = self._open.get(tick.instrument_token)
        if current is not None and bucket < current.opened_at:
            return  # Late tick: never mutate a closed candle or introduce look-ahead data.
        volume_delta = self._volume_delta(tick)
        if current is None:
            self._open[tick.instrument_token] = _OpenCandle(
                instrument_token=tick.instrument_token,
                opened_at=bucket,
                timeframe_seconds=self._timeframe_seconds,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=volume_delta,
                tick_count=1,
            )
            return
        if bucket > current.opened_at:
            await self._on_completed(current.complete())
            self._open[tick.instrument_token] = _OpenCandle(
                instrument_token=tick.instrument_token,
                opened_at=bucket,
                timeframe_seconds=self._timeframe_seconds,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=volume_delta,
                tick_count=1,
            )
            return
        current.update(tick, volume_delta)

    async def flush_expired(self, now: datetime | None = None) -> None:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        expired = [
            token
            for token, candle in self._open.items()
            if candle.opened_at + timedelta(seconds=self._timeframe_seconds) <= current_time
        ]
        for token in expired:
            candle = self._open.pop(token)
            await self._on_completed(candle.complete())


def _as_completed(row: MarketCandle) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=row.instrument_token,
        timeframe_seconds=row.timeframe_seconds,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        tick_count=row.tick_count,
    )


class MarketCalculationPersistenceService:
    """Stores a candle once, derives values, and exposes the latest snapshot in Redis."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        on_snapshot: Callable[[CompletedCandle, dict], Awaitable[None]] | None = None,
        benchmark_token: str | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._on_snapshot = on_snapshot
        self._benchmark_token = benchmark_token or settings.nifty_benchmark_token

    async def persist_completed(self, candle: CompletedCandle) -> None:
        async with SessionLocal() as session:
            existing = await session.scalar(
                select(MarketCandle).where(
                    MarketCandle.instrument_token == candle.instrument_token,
                    MarketCandle.timeframe_seconds == candle.timeframe_seconds,
                    MarketCandle.opened_at == candle.opened_at,
                )
            )
            if existing is not None:
                return
            row = MarketCandle(
                instrument_token=candle.instrument_token,
                timeframe_seconds=candle.timeframe_seconds,
                session_date=candle.session_date,
                opened_at=candle.opened_at,
                closed_at=candle.closed_at,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                tick_count=candle.tick_count,
            )
            session.add(row)
            try:
                await session.flush()
            except Exception:
                await session.rollback()
            current_rows = list(
                (
                    await session.scalars(
                        select(MarketCandle)
                        .where(
                            MarketCandle.instrument_token == candle.instrument_token,
                            MarketCandle.timeframe_seconds == candle.timeframe_seconds,
                            MarketCandle.session_date == candle.session_date,
                        )
                        .order_by(MarketCandle.opened_at)
                    )
                ).all()
            )
            benchmark_rows = current_rows
            if candle.instrument_token != self._benchmark_token:
                benchmark_rows = list(
                    (
                        await session.scalars(
                            select(MarketCandle)
                            .where(
                                MarketCandle.instrument_token == self._benchmark_token,
                                MarketCandle.timeframe_seconds == candle.timeframe_seconds,
                                MarketCandle.session_date == candle.session_date,
                            )
                            .order_by(MarketCandle.opened_at)
                        )
                    ).all()
                )
            values = indicator_snapshot(
                [_as_completed(item) for item in current_rows],
                [_as_completed(item) for item in benchmark_rows],
                opening_range_minutes=self._settings.opening_range_minutes,
                fast_ema_period=self._settings.ema_fast_period,
                slow_ema_period=self._settings.ema_slow_period,
                volume_lookback=self._settings.volume_lookback_candles,
                is_nifty=candle.instrument_token == self._benchmark_token,
            )
            session.add(
                MarketIndicatorSnapshot(
                    instrument_token=candle.instrument_token,
                    timeframe_seconds=candle.timeframe_seconds,
                    session_date=candle.session_date,
                    candle_opened_at=candle.opened_at,
                    values=values,
                )
            )
            await session.commit()
        await self._redis.set(
            f"market:indicator:{candle.instrument_token}",
            json.dumps(values),
            ex=60 * 60 * 18,
        )
        await update_outcomes(candle)
        if self._on_snapshot:
            await self._on_snapshot(candle, values)
