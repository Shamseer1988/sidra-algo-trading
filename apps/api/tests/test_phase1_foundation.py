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

    async def publish(self, _: str, __: str) -> int:
        return 0


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


async def test_signal_block_reason_handles_zero_cooldown_per_side_and_daily_ceiling() -> None:
    """P0 regression: a zero cooldown must not raise, and per-side / ceiling caps must apply."""
    from types import SimpleNamespace

    from sqlalchemy import delete

    from app.db.models import PaperSignal, ScannerEvaluation
    from app.db.session import SessionLocal, engine
    from app.services.strategy_registry import StrategyConfiguration

    await engine.dispose()
    session_date = date(2031, 3, 4)
    strategy = StrategyConfiguration(
        id="p0-block-test",
        name="P0 Block Test",
        max_trades_per_day=5,
        max_trades_per_side=1,
        cooldown_minutes=0,
    )
    controls = {"maximum_signals": 2}
    orchestrator = PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]

    def _candle(minute: int) -> CompletedCandle:
        opened_at = datetime(2031, 3, 4, 4, minute, tzinfo=UTC)
        return CompletedCandle(
            instrument_token="NSE_EQ|BLOCK",
            timeframe_seconds=60,
            opened_at=opened_at,
            closed_at=opened_at + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=100,
            tick_count=2,
        )

    long_decision = SimpleNamespace(side="LONG")
    short_decision = SimpleNamespace(side="SHORT")
    try:
        # Zero cooldown + empty ledger: no UnboundLocalError, nothing blocks the signal.
        assert await orchestrator._signal_block_reason(_candle(0), strategy, long_decision, controls) is None

        async with SessionLocal() as session:
            session.add(
                ScannerEvaluation(
                    evaluation_key="p0-block-accepted-long",
                    instrument_token="NSE_EQ|BLOCK",
                    session_date=session_date,
                    candle_opened_at=datetime(2031, 3, 4, 4, 0, tzinfo=UTC),
                    strategy_id=strategy.id,
                    strategy_name=strategy.name,
                    strategy_version=strategy.version,
                    status="ACCEPTED",
                    decision_state="SIGNALLED",
                    side="LONG",
                    reason="Paper signal confirmed",
                    failed_conditions=[],
                    data_quality_state="GOOD",
                    candle_close=Decimal("100"),
                    candle_volume=100,
                    score=100,
                )
            )
            await session.commit()

        # One accepted LONG already: the per-side cap blocks another LONG but not a SHORT.
        assert (
            await orchestrator._signal_block_reason(_candle(1), strategy, long_decision, controls)
            == "Strategy maximum long paper trades reached"
        )
        assert await orchestrator._signal_block_reason(_candle(1), strategy, short_decision, controls) is None

        # Two live paper signals recorded: the account-wide daily ceiling blocks every side.
        async with SessionLocal() as session:
            for index in range(2):
                session.add(
                    PaperSignal(
                        signal_key=f"p0-block-signal-{index}",
                        instrument_token="NSE_EQ|BLOCK",
                        session_date=session_date,
                        candle_opened_at=datetime(2031, 3, 4, 4, index, tzinfo=UTC),
                        strategy_version="orb-retest-v1@1",
                        side="SHORT",
                        entry_price=Decimal("100"),
                        stop_price=Decimal("101"),
                        target_price=Decimal("98"),
                        quantity=1,
                        risk_amount=Decimal("1"),
                        score=100,
                    )
                )
            await session.commit()
        assert (
            await orchestrator._signal_block_reason(_candle(2), strategy, short_decision, controls)
            == "Daily paper-signal ceiling reached"
        )
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperSignal).where(PaperSignal.session_date == session_date))
            await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == session_date))
            await session.commit()
        await engine.dispose()


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
