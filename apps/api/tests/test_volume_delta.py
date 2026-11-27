"""Live candle volume from the feed's cumulative day volume.

On 26 November RELIANCE's live 1-minute candles summed to 2.05 billion shares
against the provider's 20.0 million. A tick whose cumulative volume was lower
than the one before -- an older snapshot that arrived late, because websocket
messages are handled as interleaving coroutines -- was booked as a reset with
the whole day's volume, and the next tick then added the gap again.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.candle_aggregation import CandleAggregationService, MarketTick
from app.services.market_calculations import CompletedCandle

TOKEN = "NSE_EQ|INEVOLTEST"
OPEN = datetime(2026, 11, 30, 3, 45, tzinfo=UTC)  # 9:15 AM IST


def tick(seconds: int, cumulative: int | None, day: datetime = OPEN) -> MarketTick:
    return MarketTick(TOKEN, Decimal("100"), cumulative, day + timedelta(seconds=seconds))


async def candles_from(ticks: list[MarketTick]) -> list[CompletedCandle]:
    emitted: list[CompletedCandle] = []

    async def on_candle(candle: CompletedCandle) -> None:
        emitted.append(candle)

    aggregation = CandleAggregationService(60, on_candle)
    for item in ticks:
        await aggregation.consume(item)
    await aggregation.flush_expired(now=ticks[-1].exchange_timestamp + timedelta(minutes=5))
    return emitted


async def test_a_late_lower_cumulative_volume_adds_nothing() -> None:
    # In order the day reaches 1,300; one stale 1,050 snapshot arrives late.
    ticks = [tick(1, 1_000), tick(10, 1_100), tick(20, 1_200), tick(25, 1_050), tick(30, 1_300)]
    candles = await candles_from(ticks)
    # 1,000 is the baseline; then +100, +100, +0 (stale), +100.
    assert sum(c.volume for c in candles) == 300


async def test_interleaved_messages_cannot_multiply_the_day() -> None:
    # Two messages' ticks interleaved all minute: the old code booked the whole
    # cumulative figure (about 1 million) every time the order flipped.
    ticks = []
    for i in range(0, 58, 2):
        ticks.append(tick(i, 1_000_000 + i * 10 + 10))
        ticks.append(tick(i + 1, 1_000_000 + i * 10))  # the older snapshot, second
    candles = await candles_from(ticks)
    assert sum(c.volume for c in candles) == (1_000_000 + 570) - (1_000_010)


async def test_a_new_session_resets_and_counts_its_volume_so_far() -> None:
    yesterday = OPEN - timedelta(days=3)  # Friday 27 November
    ticks = [tick(1, 5_000_000, yesterday), tick(2, 5_000_100, yesterday), tick(1, 2_000), tick(5, 2_500)]
    candles = await candles_from(ticks)
    today = [c for c in candles if c.opened_at >= OPEN]
    assert sum(c.volume for c in today) == 2_500


async def test_a_tick_without_volume_changes_nothing() -> None:
    candles = await candles_from([tick(1, 1_000), tick(5, None), tick(10, 1_400)])
    assert sum(c.volume for c in candles) == 400
