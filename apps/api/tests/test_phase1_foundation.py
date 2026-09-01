import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.candle_aggregation import MarketTick
from app.services.data_quality import DataQualityState, MarketDataQualityService
from app.services.market_calculations import CompletedCandle, is_regular_market_timestamp
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.trading_calendar import MarketPhase, TradingCalendar, TradingSession
from app.services.worker_supervision import RestartBackoff, completed_task_detail


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **_: object) -> bool:
        self.values[key] = value
        return True


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://sidra:secret@postgres/sidra",
        "redis_url": "redis://redis:6379/0",
        "jwt_secret": "a-secure-test-secret-that-is-longer-than-32-characters",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def candle(opened_at: datetime, instrument: str = "NSE_EQ|TEST") -> CompletedCandle:
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=100,
        tick_count=2,
    )


def tick(exchange_timestamp: datetime, received_timestamp: datetime | None = None) -> MarketTick:
    return MarketTick(
        instrument_token="NSE_EQ|TEST",
        price=Decimal("100.5"),
        cumulative_volume=100,
        exchange_timestamp=exchange_timestamp,
        received_timestamp=received_timestamp or exchange_timestamp,
    )


def test_trading_calendar_classifies_regular_weekend_holiday_and_all_phases() -> None:
    calendar = TradingCalendar()
    assert calendar.status_at(datetime(2026, 8, 31, 3, 20, tzinfo=UTC)).phase == MarketPhase.CLOSED
    assert calendar.status_at(datetime(2026, 8, 31, 3, 35, tzinfo=UTC)).phase == MarketPhase.PRE_OPEN
    assert calendar.status_at(datetime(2026, 8, 31, 3, 45, tzinfo=UTC)).phase == MarketPhase.OPEN
    assert calendar.status_at(datetime(2026, 8, 31, 10, 15, tzinfo=UTC)).phase == MarketPhase.POST_MARKET
    assert calendar.status_at(datetime(2026, 9, 5, 4, 0, tzinfo=UTC)).reason == "Weekend"
    holiday = calendar.status_at(datetime(2026, 9, 14, 4, 0, tzinfo=UTC))
    assert holiday.phase == MarketPhase.CLOSED
    assert holiday.reason == "Ganesh Chaturthi"
    assert not is_regular_market_timestamp(datetime(2026, 9, 14, 4, 0, tzinfo=UTC), calendar)


def test_trading_calendar_supports_special_sessions_and_fails_closed_without_coverage() -> None:
    muhurat_date = date(2026, 11, 8)
    special = TradingSession(
        trading_date=muhurat_date,
        pre_open=time(18, 0),
        regular_open=time(18, 0),
        regular_close=time(19, 0),
        post_market_close=time(19, 0),
        is_special=True,
        label="Muhurat trading",
    )
    calendar = TradingCalendar(special_sessions={muhurat_date: special})
    status = calendar.status_at(datetime(2026, 11, 8, 13, 0, tzinfo=UTC))
    assert status.phase == MarketPhase.OPEN
    assert status.session and status.session.is_special
    unknown = calendar.status_at(datetime(2027, 1, 4, 4, 0, tzinfo=UTC))
    assert unknown.phase == MarketPhase.CLOSED
    assert "not confirmed" in unknown.reason


async def test_data_quality_is_good_when_expected_bar_and_fresh_tick_are_present() -> None:
    redis = MemoryRedis()
    service = MarketDataQualityService(settings(), redis, TradingCalendar())  # type: ignore[arg-type]
    opened_at = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    await service.observe_tick(tick(opened_at + timedelta(minutes=1, seconds=1)))
    snapshot = await service.observe_completed(candle(opened_at))
    assert snapshot.state == DataQualityState.GOOD
    assert snapshot.expected_bars == 1
    assert snapshot.received_bars == 1
    assert snapshot.allows_signals


async def test_data_quality_blocks_missing_bars_and_stale_ticks() -> None:
    opened_at = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    missing_service = MarketDataQualityService(settings(), MemoryRedis(), TradingCalendar())  # type: ignore[arg-type]
    await missing_service.observe_tick(tick(opened_at + timedelta(minutes=3, seconds=1)))
    missing = await missing_service.observe_completed(candle(opened_at + timedelta(minutes=2)))
    assert missing.state == DataQualityState.INVALID
    assert len(missing.missing_buckets) == 2
    assert not missing.allows_signals

    stale_service = MarketDataQualityService(
        settings(data_quality_max_missing_bars=10),
        MemoryRedis(),
        TradingCalendar(),  # type: ignore[arg-type]
    )
    await stale_service.observe_tick(tick(opened_at + timedelta(seconds=1)))
    stale = stale_service.snapshot("NSE_EQ|TEST", opened_at + timedelta(minutes=1))
    assert stale.state == DataQualityState.STALE
    assert not stale.allows_signals


async def test_data_quality_tracks_duplicates_as_degraded_but_not_invalid() -> None:
    opened_at = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    service = MarketDataQualityService(settings(), MemoryRedis(), TradingCalendar())  # type: ignore[arg-type]
    next_tick = tick(opened_at + timedelta(minutes=1, seconds=1))
    await service.observe_tick(next_tick)
    await service.observe_tick(next_tick)
    snapshot = await service.observe_completed(candle(opened_at))
    assert snapshot.state == DataQualityState.DEGRADED
    assert snapshot.duplicate_ticks == 1
    assert snapshot.allows_signals


async def test_scanner_fails_closed_without_a_valid_data_quality_snapshot() -> None:
    orchestrator = PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]
    await orchestrator.on_completed_candle(
        candle(datetime(2026, 8, 31, 4, 0, tzinfo=UTC)),
        {"data_quality": {"state": "INVALID", "reason": "missing bars"}},
    )


def test_worker_restart_backoff_is_bounded_and_resettable() -> None:
    backoff = RestartBackoff(initial_seconds=2, maximum_seconds=8)
    assert backoff.record_failure(now=10) == 2
    assert not backoff.ready(now=11)
    assert backoff.record_failure(now=20) == 4
    assert backoff.record_failure(now=30) == 8
    assert backoff.record_failure(now=40) == 8
    backoff.reset()
    assert backoff.ready(now=0)
    assert backoff.failures == 0


async def test_worker_task_outcome_retains_exception_detail() -> None:
    async def fail() -> None:
        raise RuntimeError("feed disconnected")

    task = asyncio.create_task(fail())
    await asyncio.gather(task, return_exceptions=True)
    assert completed_task_detail(task) == "RuntimeError: feed disconnected"


def test_production_configuration_fails_closed() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        settings(app_env="production", web_origin="https://sidra.example.com", cookie_secure=False)
    production = settings(
        app_env="production",
        web_origin="https://sidra.example.com",
        cookie_secure=True,
        auto_create_schema=False,
    )
    assert production.live_trading_enabled is False
    with pytest.raises(ValidationError, match="LIVE_TRADING_ENABLED"):
        settings(live_trading_enabled=True)


def test_invalid_calendar_overrides_fail_during_configuration() -> None:
    with pytest.raises(ValidationError, match="Invalid NSE holiday"):
        settings(nse_special_sessions="2026-11-08@19:00-18:00")
