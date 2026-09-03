from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.services.candle_aggregation import CandleAggregationService, MarketTick
from app.services.market_calculations import (
    CompletedCandle,
    atr,
    daily_atr,
    ema,
    indicator_snapshot,
    nifty_regime,
    opening_range,
    prior_day_levels,
    relative_strength,
    session_candles,
    session_vwap,
    time_of_day_relative_volume,
    true_ranges,
    volume_metrics,
    vwap_bands,
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


def _ohlc(open_p: str, high: str, low: str, close: str, *, volume: int = 100) -> CompletedCandle:
    opened_at = datetime(2026, 8, 31, 9, 15, tzinfo=IST).astimezone(UTC)
    return CompletedCandle(
        instrument_token="NSE:TEST",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(open_p),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        tick_count=1,
    )


def test_true_range_uses_prior_close_gaps() -> None:
    candles = [
        _ohlc("10", "12", "9", "11"),
        _ohlc("11", "20", "18", "19"),
        _ohlc("19", "19.5", "15", "16"),
    ]
    ranges = true_ranges(candles)
    assert ranges[0] == Decimal("3")  # first bar: high - low only
    assert ranges[1] == Decimal("9")  # max(2, |20-11|, |18-11|)
    assert ranges[2] == Decimal("4.5")  # max(4.5, |19.5-19|, |15-19|)


def test_atr_is_wilder_smoothed_and_requires_enough_closed_bars() -> None:
    flat = [make_candle(index, str(100 + index)) for index in range(6)]  # every true range is 4
    assert atr(flat, 3) == Decimal("4")
    assert atr(flat[:3], 3) is None  # only two gap-aware ranges
    assert atr([], 14) is None


def test_vwap_bands_are_symmetric_around_session_vwap() -> None:
    candles = [make_candle(0, "100"), make_candle(1, "110"), make_candle(2, "90")]
    bands = vwap_bands(candles)
    assert bands["vwap"] == pytest.approx(float(session_vwap(candles)))
    assert bands["sigma"] > 0
    assert bands["upper_1"] - bands["vwap"] == pytest.approx(bands["sigma"], abs=1e-3)
    assert bands["vwap"] - bands["lower_1"] == pytest.approx(bands["sigma"], abs=1e-3)
    assert bands["upper_2"] - bands["vwap"] == pytest.approx(2 * bands["sigma"], abs=1e-3)
    assert vwap_bands([])["vwap"] is None


def _daily(day: int, open_p: str, high: str, low: str, close: str, *, volume: int = 1_000_000) -> CompletedCandle:
    opened_at = datetime(2026, 8, 3, 9, 15, tzinfo=IST).astimezone(UTC) + timedelta(days=day)
    return CompletedCandle(
        instrument_token="NSE:TEST",
        timeframe_seconds=86_400,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=6),
        open=Decimal(open_p),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        tick_count=1,
    )


def test_prior_day_levels_returns_the_most_recent_daily_candle() -> None:
    days = [
        _daily(0, "100", "102", "98", "101"),
        _daily(2, "104", "106", "101", "102"),
        _daily(1, "101", "105", "100", "104"),
    ]
    assert prior_day_levels(days) == {"close": 102.0, "high": 106.0, "low": 101.0}
    assert prior_day_levels([]) == {"close": None, "high": None, "low": None}


def test_daily_atr_reuses_the_shared_estimator() -> None:
    days = [_daily(index, "100", "104", "96", "100") for index in range(20)]  # every true range is 8
    assert daily_atr(days, 14) == Decimal("8")
    assert daily_atr(days[:3], 14) is None


def test_time_of_day_relative_volume_compares_to_the_same_minute_baseline() -> None:
    active = [make_candle(0, "100", volume=300)]  # opens 09:15 IST
    assert time_of_day_relative_volume(active, {"09:15": 150.0, "09:16": 200.0}) == 2.0
    assert time_of_day_relative_volume(active, {}) is None
    assert time_of_day_relative_volume([], {"09:15": 150.0}) is None
    assert time_of_day_relative_volume(active, {"09:15": 0.0}) is None


def test_indicator_snapshot_includes_prior_day_context_when_daily_candles_supplied() -> None:
    stock = [make_candle(index, str(100 + (index % 3))) for index in range(30)]
    benchmark = [make_candle(index, "100") for index in range(30)]
    daily = [_daily(index, "90", "95", "88", "92") for index in range(20)]
    snapshot = indicator_snapshot(
        stock,
        benchmark,
        opening_range_minutes=15,
        fast_ema_period=9,
        slow_ema_period=21,
        volume_lookback=20,
        is_nifty=False,
        atr_period=14,
        daily_candles=daily,
        time_of_day_volume_baseline={"09:44": 50.0},  # the last active bar opens at 09:44 IST
    )
    assert snapshot["prior_day"]["close"] == 92.0
    assert snapshot["daily_atr"] is not None
    assert snapshot["gap_percent"] is not None
    assert snapshot["distance_to_prior_high_atr"] is not None
    assert snapshot["time_of_day_rvol"] is not None


def test_indicator_snapshot_prior_day_context_is_absent_without_daily_candles() -> None:
    stock = [make_candle(index, "100") for index in range(25)]
    snapshot = indicator_snapshot(
        stock,
        stock,
        opening_range_minutes=15,
        fast_ema_period=9,
        slow_ema_period=21,
        volume_lookback=20,
        is_nifty=False,
        atr_period=14,
    )
    assert snapshot["prior_day"] == {"close": None, "high": None, "low": None}
    assert snapshot["gap_percent"] is None
    assert snapshot["daily_atr"] is None
    assert snapshot["time_of_day_rvol"] is None


def test_indicator_snapshot_exposes_volatility_and_structure_fields() -> None:
    stock = [make_candle(index, str(100 + (index % 3))) for index in range(30)]
    benchmark = [make_candle(index, "100") for index in range(30)]
    snapshot = indicator_snapshot(
        stock,
        benchmark,
        opening_range_minutes=15,
        fast_ema_period=9,
        slow_ema_period=21,
        volume_lookback=20,
        is_nifty=False,
        atr_period=14,
    )
    assert snapshot["atr"] is not None
    assert snapshot["atr_percent"] is not None
    assert snapshot["vwap_bands"]["vwap"] is not None
    assert snapshot["opening_range_atr"] is not None
    assert snapshot["extension_atr"] is not None


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
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, start, start + timedelta(milliseconds=80)))
    await aggregation.consume(
        MarketTick(
            "NSE:26000",
            Decimal("102"),
            150,
            start + timedelta(seconds=20),
            start + timedelta(seconds=20, milliseconds=75),
        )
    )
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
    premarket = datetime(2026, 8, 31, 3, 0, tzinfo=UTC)
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, premarket, premarket))
    await aggregation.flush_expired(datetime(2026, 8, 31, 4, 0, tzinfo=UTC))
    assert completed == []


@pytest.mark.asyncio
async def test_aggregator_ignores_late_ticks_after_a_completed_bucket() -> None:
    completed: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        completed.append(candle)

    aggregation = CandleAggregationService(60, receive)
    start = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)
    await aggregation.consume(MarketTick("NSE:26000", Decimal("100"), 100, start, start))
    await aggregation.consume(
        MarketTick("NSE:26000", Decimal("101"), 110, start + timedelta(minutes=1), start + timedelta(minutes=1))
    )
    await aggregation.consume(
        MarketTick("NSE:26000", Decimal("50"), 120, start + timedelta(seconds=30), start + timedelta(minutes=2))
    )
    assert completed[0].low == Decimal("100")


@pytest.mark.asyncio
async def test_replay_orders_candles_deterministically() -> None:
    received: list[CompletedCandle] = []

    async def receive(candle: CompletedCandle) -> None:
        received.append(candle)

    first, second = make_candle(0, "100"), make_candle(1, "101")
    assert await replay_completed_candles([second, first], receive) == 2
    assert received == [first, second]
