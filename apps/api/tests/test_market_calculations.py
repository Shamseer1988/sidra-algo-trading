from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.services.candle_aggregation import CandleAggregationService, MarketTick
from app.services.market_calculations import (
    CompletedCandle,
    ema,
    nifty_regime,
    opening_range,
    relative_strength,
    session_candles,
    session_vwap,
    volume_metrics,
)
from app.services.replay import replay_completed_candles

IST = ZoneInfo("Asia/Kolkata")


def make_candle(index: int, close: str, *, volume: int = 100) -> CompletedCandle:
    opened_at = datetime(2026, 8, 31, 9, 15, tzinfo=IST).astimezone(UTC) + timedelta(minutes=index)
    close_price = Decimal(close)
    return CompletedCandle(
        instrument_token="NSE:26000",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=close_price - Decimal("1"),
        high=close_price + Decimal("2"),
        low=close_price - Decimal("2"),
        close=close_price,
        volume=volume,
        tick_count=3,
    )


def test_completed_candle_indicators_are_session_bound() -> None:
    candles = [make_candle(0, "100", volume=100), make_candle(1, "102", volume=200), make_candle(2, "104", volume=300)]
    assert session_vwap(candles) == Decimal("102.6666666666666666666666667")
    assert ema(candles, 3) == Decimal("102.5")
    assert opening_range(candles, 2) == {"high": 104.0, "low": 98.0, "complete": True}
    assert volume_metrics(candles, 20)["relative_volume"] == 2.0


def test_relative_strength_and_nifty_regime_use_closed_history_only() -> None:
    nifty = [make_candle(index, str(100 + index * 2)) for index in range(21)]
    stock = [
        CompletedCandle(
            instrument_token="NSE:2885",
            timeframe_seconds=item.timeframe_seconds,
            opened_at=item.opened_at,
            closed_at=item.closed_at,
            open=item.open * Decimal("2"),
            high=item.high * Decimal("2"),
            low=item.low * Decimal("2"),
            close=item.close * Decimal("2.1"),
            volume=item.volume,
            tick_count=item.tick_count,
        )
        for item in nifty
    ]
    strength = relative_strength(stock, nifty)
    assert strength["relative_strength_percent"] is not None
    assert strength["relative_strength_percent"] > 0
    assert nifty_regime(nifty, 9, 21)["regime"] == "BULLISH"


def test_relative_strength_only_uses_timestamp_aligned_candles() -> None:
    benchmark = [make_candle(0, "100"), make_candle(1, "100"), make_candle(2, "100")]
    stock = [
        CompletedCandle(
            instrument_token="NSE:2885",
            timeframe_seconds=60,
            opened_at=item.opened_at,
            closed_at=item.closed_at,
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("99"),
            close=Decimal("102"),
            volume=100,
            tick_count=1,
        )
        for item in [benchmark[0], benchmark[2]]
    ]
    assert relative_strength(stock, benchmark)["relative_strength_percent"] == 0.9899


def test_session_candles_excludes_pre_and_post_market_ticks() -> None:
    regular = make_candle(0, "100")
    pre_open = CompletedCandle(
        "NSE:26000",
        60,
        regular.opened_at - timedelta(minutes=30),
        regular.opened_at - timedelta(minutes=29),
        Decimal("90"),
        Decimal("91"),
        Decimal("89"),
        Decimal("90"),
        100,
        1,
    )
    post_close = CompletedCandle(
        "NSE:26000",
        60,
        regular.opened_at.replace(hour=10, minute=1) + timedelta(hours=6),
        regular.opened_at.replace(hour=10, minute=2) + timedelta(hours=6),
        Decimal("110"),
        Decimal("111"),
        Decimal("109"),
        Decimal("110"),
        100,
        1,
    )
    assert session_candles([pre_open, regular, post_close]) == [regular]


@pytest.mark.asyncio
async def test_aggregator_emits_only_after_candle_closes() -> None:
    completed: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        completed.append(candle)

    aggregation = CandleAggregationService(60, receive)
    start = datetime(2026, 8, 31, 3, 45, 1, tzinfo=UTC)
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, start))
    await aggregation.consume(MarketTick("NSE:26000", Decimal("102"), 150, start + timedelta(seconds=20)))
    assert completed == []
    await aggregation.flush_expired(start + timedelta(seconds=61))
    assert len(completed) == 1
    assert completed[0].open == Decimal("100")
    assert completed[0].close == Decimal("102")
    assert completed[0].volume == 50


@pytest.mark.asyncio
async def test_aggregator_rejects_ticks_outside_regular_market_session() -> None:
    completed: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        completed.append(candle)

    aggregation = CandleAggregationService(60, receive)
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, datetime(2026, 8, 31, 3, 0, tzinfo=UTC)))
    await aggregation.flush_expired(datetime(2026, 8, 31, 4, 0, tzinfo=UTC))
    assert completed == []


@pytest.mark.asyncio
async def test_aggregator_ignores_late_ticks_after_a_completed_bucket() -> None:
    completed: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        completed.append(candle)

    aggregation = CandleAggregationService(60, receive)
    start = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, start))
    await aggregation.consume(MarketTick("NSE:26000", Decimal("101"), 110, start + timedelta(minutes=1)))
    await aggregation.consume(MarketTick("NSE:26000", Decimal("50"), 120, start + timedelta(seconds=30)))
    assert completed[0].low == Decimal("100")


@pytest.mark.asyncio
async def test_replay_orders_candles_deterministically() -> None:
    received: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        received.append(candle)

    first, second = make_candle(0, "100"), make_candle(1, "101")
    assert await replay_completed_candles([second, first], receive) == 2
    assert received == [first, second]
