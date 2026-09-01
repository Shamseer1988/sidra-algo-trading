"""Per-instrument market-data quality metrics and signal safety gates."""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from redis.asyncio import Redis

from app.core.config import Settings
from app.services.market_calculations import CompletedCandle
from app.services.trading_calendar import MARKET_TIMEZONE, MarketPhase, TradingCalendar

DATA_QUALITY_PREFIX = "market:data_quality:"
DATA_QUALITY_TTL_SECONDS = 60 * 60 * 18


class DataQualityState(StrEnum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INVALID = "INVALID"


class MarketTickLike(Protocol):
    instrument_token: str
    price: Decimal
    cumulative_volume: int | None
    exchange_timestamp: datetime
    received_timestamp: datetime | None


@dataclass
class _InstrumentMetrics:
    session_date: date
    received_ticks: int = 0
    duplicate_ticks: int = 0
    out_of_order_ticks: int = 0
    invalid_ticks: int = 0
    total_latency_ms: int = 0
    max_latency_ms: int = 0
    last_exchange_timestamp: datetime | None = None
    last_received_timestamp: datetime | None = None
    last_fingerprint: tuple[str, str, int | None] | None = None
    received_bar_buckets: set[datetime] = field(default_factory=set)
    last_published_at: datetime | None = None


@dataclass(frozen=True)
class DataQualitySnapshot:
    instrument_token: str
    state: DataQualityState
    reason: str
    session_date: date
    expected_bars: int
    received_bars: int
    missing_buckets: tuple[str, ...]
    received_ticks: int
    duplicate_ticks: int
    out_of_order_ticks: int
    invalid_ticks: int
    average_latency_ms: int
    max_latency_ms: int
    last_exchange_timestamp: datetime | None
    last_received_timestamp: datetime | None
    observed_at: datetime

    @property
    def allows_signals(self) -> bool:
        return self.state not in {DataQualityState.INVALID, DataQualityState.STALE}

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_token": self.instrument_token,
            "state": self.state.value,
            "reason": self.reason,
            "session_date": self.session_date.isoformat(),
            "expected_bars": self.expected_bars,
            "received_bars": self.received_bars,
            "missing_buckets": list(self.missing_buckets),
            "received_ticks": self.received_ticks,
            "duplicate_ticks": self.duplicate_ticks,
            "out_of_order_ticks": self.out_of_order_ticks,
            "invalid_ticks": self.invalid_ticks,
            "average_latency_ms": self.average_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "last_exchange_timestamp": (
                self.last_exchange_timestamp.isoformat() if self.last_exchange_timestamp else None
            ),
            "last_received_timestamp": (
                self.last_received_timestamp.isoformat() if self.last_received_timestamp else None
            ),
            "observed_at": self.observed_at.isoformat(),
            "allows_signals": self.allows_signals,
        }


def data_quality_key(instrument_token: str) -> str:
    return f"{DATA_QUALITY_PREFIX}{instrument_token}"


class MarketDataQualityService:
    """Tracks ticks/bars and publishes a bounded Redis quality snapshot."""

    def __init__(self, settings: Settings, redis: Redis, calendar: TradingCalendar) -> None:
        self._settings = settings
        self._redis = redis
        self._calendar = calendar
        self._metrics: dict[str, _InstrumentMetrics] = {}

    def _for_session(self, instrument_token: str, session_date: date) -> _InstrumentMetrics:
        metrics = self._metrics.get(instrument_token)
        if metrics is None or metrics.session_date != session_date:
            metrics = _InstrumentMetrics(session_date=session_date)
            self._metrics[instrument_token] = metrics
        return metrics

    async def observe_tick(self, tick: MarketTickLike) -> DataQualitySnapshot:
        exchange_timestamp = tick.exchange_timestamp
        received_timestamp = tick.received_timestamp or exchange_timestamp
        instrument_token = str(tick.instrument_token)
        metrics = self._for_session(instrument_token, exchange_timestamp.astimezone(MARKET_TIMEZONE).date())
        metrics.received_ticks += 1
        price = tick.price
        cumulative_volume = tick.cumulative_volume
        fingerprint = (exchange_timestamp.isoformat(), str(price), cumulative_volume)
        if metrics.last_fingerprint == fingerprint:
            metrics.duplicate_ticks += 1
        if metrics.last_exchange_timestamp and exchange_timestamp < metrics.last_exchange_timestamp:
            metrics.out_of_order_ticks += 1
        if not instrument_token or price <= 0 or exchange_timestamp > received_timestamp + timedelta(seconds=2):
            metrics.invalid_ticks += 1
        metrics.last_fingerprint = fingerprint
        metrics.last_exchange_timestamp = max(
            exchange_timestamp,
            metrics.last_exchange_timestamp or exchange_timestamp,
        )
        metrics.last_received_timestamp = max(
            received_timestamp,
            metrics.last_received_timestamp or received_timestamp,
        )
        latency_ms = max(int((received_timestamp - exchange_timestamp).total_seconds() * 1000), 0)
        metrics.total_latency_ms += latency_ms
        metrics.max_latency_ms = max(metrics.max_latency_ms, latency_ms)
        snapshot = self.snapshot(instrument_token, received_timestamp)
        if metrics.last_published_at is None or received_timestamp - metrics.last_published_at >= timedelta(seconds=1):
            await self._publish(snapshot)
            metrics.last_published_at = received_timestamp
        return snapshot

    async def observe_completed(self, candle: CompletedCandle) -> DataQualitySnapshot:
        metrics = self._for_session(candle.instrument_token, candle.session_date)
        metrics.received_bar_buckets.add(candle.opened_at)
        as_of = metrics.last_received_timestamp or candle.closed_at
        snapshot = self.snapshot(candle.instrument_token, as_of)
        await self._publish(snapshot)
        metrics.last_published_at = as_of
        return snapshot

    def snapshot(self, instrument_token: str, as_of: datetime | None = None) -> DataQualitySnapshot:
        observed_at = (as_of or datetime.now(UTC)).astimezone(UTC)
        local_date = observed_at.astimezone(MARKET_TIMEZONE).date()
        metrics = self._for_session(instrument_token, local_date)
        calendar_status = self._calendar.status_at(observed_at)
        session = self._calendar.session_for(metrics.session_date)
        expected: set[datetime] = set()
        if session is not None:
            local_as_of = observed_at.astimezone(MARKET_TIMEZONE)
            session_open = datetime.combine(metrics.session_date, session.regular_open, tzinfo=MARKET_TIMEZONE)
            session_close = datetime.combine(metrics.session_date, session.regular_close, tzinfo=MARKET_TIMEZONE)
            effective_end = min(local_as_of, session_close)
            bucket_count = max(
                int((effective_end - session_open).total_seconds()) // self._settings.candle_timeframe_seconds,
                0,
            )
            expected = {
                (session_open + timedelta(seconds=index * self._settings.candle_timeframe_seconds)).astimezone(UTC)
                for index in range(bucket_count)
            }
        missing = tuple(sorted(item.isoformat() for item in expected - metrics.received_bar_buckets))
        average_latency = int(metrics.total_latency_ms / metrics.received_ticks) if metrics.received_ticks else 0
        duplicate_percent = (metrics.duplicate_ticks / metrics.received_ticks) * 100 if metrics.received_ticks else 0

        if session is None:
            state = DataQualityState.INVALID
            reason = self._calendar.closed_reason(metrics.session_date)
        elif metrics.invalid_ticks:
            state = DataQualityState.INVALID
            reason = "Invalid tick values or timestamps detected"
        elif len(missing) > self._settings.data_quality_max_missing_bars:
            state = DataQualityState.INVALID
            reason = f"{len(missing)} expected completed candle bucket(s) are missing"
        elif calendar_status.phase == MarketPhase.OPEN and (
            metrics.last_received_timestamp is None
            or observed_at - metrics.last_received_timestamp
            > timedelta(seconds=self._settings.data_quality_stale_after_seconds)
        ):
            state = DataQualityState.STALE
            reason = "No fresh market tick within the configured stale threshold"
        elif (
            metrics.max_latency_ms > self._settings.data_quality_max_tick_latency_ms
            or duplicate_percent > self._settings.data_quality_max_duplicate_percent
            or metrics.out_of_order_ticks > 0
        ):
            state = DataQualityState.DEGRADED
            reason = "Feed latency, duplicates, or out-of-order ticks exceed the preferred quality level"
        else:
            state = DataQualityState.GOOD
            reason = "Expected bars and tick freshness are within configured limits"

        return DataQualitySnapshot(
            instrument_token=instrument_token,
            state=state,
            reason=reason,
            session_date=metrics.session_date,
            expected_bars=len(expected),
            received_bars=len(metrics.received_bar_buckets & expected),
            missing_buckets=missing,
            received_ticks=metrics.received_ticks,
            duplicate_ticks=metrics.duplicate_ticks,
            out_of_order_ticks=metrics.out_of_order_ticks,
            invalid_ticks=metrics.invalid_ticks,
            average_latency_ms=average_latency,
            max_latency_ms=metrics.max_latency_ms,
            last_exchange_timestamp=metrics.last_exchange_timestamp,
            last_received_timestamp=metrics.last_received_timestamp,
            observed_at=observed_at,
        )

    async def _publish(self, snapshot: DataQualitySnapshot) -> None:
        await self._redis.set(
            data_quality_key(snapshot.instrument_token),
            json.dumps(snapshot.as_dict()),
            ex=DATA_QUALITY_TTL_SECONDS,
        )
