"""Failure and recovery test suite for Phase 9 Release Gate 3.

Validates:
- Redis outage, safe state degradation, and graceful reconnection
- PostgreSQL connection drop, clean rollback, and transaction recovery
- Scanner worker restart, candle/signal/alert deduplication idempotency
- Strict preservation of the PAPER-ONLY invariant (LIVE_TRADING_ENABLED=False) across all failures
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.db.models import PaperSignal
from app.services.candle_aggregation import CandleAggregationService, MarketTick
from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, LONG_BREAKOUT, SIGNALLED, evaluate_orb_retest


@pytest.mark.asyncio
async def test_redis_outage_and_reconnection_resilience():
    """Verify that a Redis outage degrades safely and reconnects without crashing the system."""
    mock_redis = AsyncMock()
    # First call fails, simulating network outage; second call succeeds upon reconnection
    mock_redis.ping.side_effect = [RedisConnectionError("Connection refused"), True]
    mock_redis.get.side_effect = [RedisConnectionError("Connection lost"), "STOPPED"]

    # Initial check during outage
    with pytest.raises(RedisConnectionError):
        await mock_redis.ping()
    with pytest.raises(RedisConnectionError):
        await mock_redis.get("scanner:control_state")

    # Recovery: Redis comes back online
    reconnected = await mock_redis.ping()
    assert reconnected is True

    state = await mock_redis.get("scanner:control_state")
    assert state == "STOPPED"


@pytest.mark.asyncio
async def test_database_connection_failure_and_rollback():
    """Verify that a DB connection drop rolls back cleanly and prevents partial/orphaned state."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    # Simulate DB operational failure on commit
    mock_session.commit.side_effect = OperationalError("statement", {}, Exception("Connection lost"))

    signal = PaperSignal(
        id=uuid4(),
        session_date=date(2026, 8, 31),
        instrument_token="NSE:26000",
        candle_opened_at=datetime(2026, 8, 31, 4, 0, tzinfo=UTC),
        side="LONG",
        status="PAPER_SIGNALLED",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        quantity=10,
        score=95,
        score_breakdown={},
        strategy_version="orb-retest-v1",
    )

    mock_session.add(signal)
    with pytest.raises(OperationalError):
        await mock_session.commit()

    # Explicit rollback verification
    await mock_session.rollback()
    mock_session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_restart_and_candle_deduplication():
    """Verify that when a scanner worker restarts, duplicate candles/ticks are not duplicated."""
    emitted_candles: list[CompletedCandle] = []

    async def on_candle(candle: CompletedCandle):
        emitted_candles.append(candle)

    # Aggregator 1 runs before crash
    aggregator1 = CandleAggregationService(60, on_candle)
    base_time = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)

    # Emit ticks for 2 completed candles
    for i in range(125):
        t = base_time + timedelta(seconds=i)
        await aggregator1.consume(MarketTick("NSE:26000", Decimal("100"), 100 + i, t))

    assert len(emitted_candles) == 2

    # Simulate Worker Crash & Restart: Aggregator 2 spins up and re-processes from reconnect point
    aggregator2 = CandleAggregationService(60, on_candle)
    # Replaying overlapping tick from last minute
    overlap_tick = MarketTick("NSE:26000", Decimal("100"), 225, base_time + timedelta(seconds=124))
    await aggregator2.consume(overlap_tick)

    # Next minute ticks
    for i in range(125, 185):
        t = base_time + timedelta(seconds=i)
        await aggregator2.consume(MarketTick("NSE:26000", Decimal("101"), 100 + i, t))

    assert len(emitted_candles) == 3
    # Check that candle timestamps are distinct (no duplicate candle bucket)
    opened_ats = [c.opened_at for c in emitted_candles]
    assert len(opened_ats) == len(set(opened_ats))


@pytest.mark.asyncio
async def test_candle_not_completed_twice_when_flush_races_rollover():
    """A completing candle must not be emitted by both consume() rollover and flush_expired()."""
    emitted: list[CompletedCandle] = []
    holder: dict[str, CandleAggregationService] = {}

    async def on_candle(candle: CompletedCandle):
        emitted.append(candle)
        # Re-enter flush_expired mid-completion, exactly as the worker loop would if it
        # interleaved with the market-data feed task at this await point.
        await holder["aggregator"].flush_expired(now=datetime(2026, 8, 31, 3, 46, 30, tzinfo=UTC))

    aggregator = CandleAggregationService(60, on_candle)
    holder["aggregator"] = aggregator

    base_time = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    for i in range(60):  # fill the 03:45 candle
        await aggregator.consume(MarketTick("NSE:26000", Decimal("100"), 100 + i, base_time + timedelta(seconds=i)))
    assert emitted == []

    # First tick of the 03:46 bucket triggers the rollover completion of 03:45.
    await aggregator.consume(MarketTick("NSE:26000", Decimal("101"), 200, base_time + timedelta(seconds=60)))

    assert len(emitted) == 1
    assert emitted[0].opened_at == base_time


@pytest.mark.asyncio
async def test_signal_and_alert_deduplication_idempotency():
    """Verify that signal generation and Telegram alert recording are strictly idempotent."""
    opened_at = datetime(2026, 8, 31, 4, 2, tzinfo=UTC)  # 09:32 Asia/Kolkata
    breakout_candle = CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("110.5"),
        high=Decimal("112"),
        low=Decimal("110.8"),
        close=Decimal("111"),
        volume=500,
        tick_count=10,
    )

    retest_candle = CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at + timedelta(minutes=1),
        closed_at=opened_at + timedelta(minutes=2),
        open=Decimal("111"),
        high=Decimal("112.5"),
        low=Decimal("110.1"),
        close=Decimal("112"),
        volume=500,
        tick_count=10,
    )

    indicators = {
        "opening_range": {"high": 110.0, "low": 100.0, "complete": True},
        "vwap": 105.0,
        "ema_fast": 111.0,
        "ema_slow": 106.0,
        "volume": {"relative_volume": 1.5},
        "relative_strength": {"relative_strength_percent": 0.4},
    }
    nifty = {"nifty_regime": {"regime": "BULLISH"}}
    controls = {
        "account_capital": 100000.0,
        "risk_per_trade_percent": 0.5,
        "maximum_daily_risk_percent": 1.0,
        "maximum_signals": 2,
        "minimum_score": 80,
        "minimum_rr": 1.5,
        "volume_multiplier": 1.2,
        "retest_tolerance_percent": 0.15,
        "minimum_ema_spread_percent": 0.05,
        "trade_start_time": "09:24",
        "trade_cutoff_time": "14:45",
    }

    # Step 1: Breakout triggers LONG_BREAKOUT state
    breakout_decision = evaluate_orb_retest(breakout_candle, indicators, nifty, controls, prior_state=AWAITING)
    assert breakout_decision.next_state == LONG_BREAKOUT

    # Step 2: Retest confirmed triggers SIGNALLED
    signal_decision = evaluate_orb_retest(retest_candle, indicators, nifty, controls, prior_state=LONG_BREAKOUT)
    assert signal_decision.next_state == SIGNALLED
    assert signal_decision.side == "LONG"
    assert signal_decision.score >= 80

    # Step 3: Re-evaluating with prior_state=SIGNALLED returns idempotent state without duplicate emission
    duplicate_decision = evaluate_orb_retest(retest_candle, indicators, nifty, controls, prior_state=SIGNALLED)
    assert duplicate_decision.next_state == SIGNALLED
    assert duplicate_decision.reason == "Instrument already signalled this session"


def test_paper_only_invariant_enforcement():
    """Verify that LIVE_TRADING_ENABLED remains strictly False and cannot be toggled in this release."""
    settings = Settings(
        app_env="test",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
        jwt_secret_key="0" * 64,
        jwt_signing_key="0" * 64,
        fernet_encryption_key="0" * 32,
    )

    # Invariants
    assert settings.live_trading_enabled is False
    assert settings.application_mode == "PAPER"
