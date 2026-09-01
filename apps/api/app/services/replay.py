"""Deterministic completed-candle replay for paper-strategy verification."""

from collections.abc import Awaitable, Callable

from app.services.market_calculations import CompletedCandle


async def replay_completed_candles(
    candles: list[CompletedCandle],
    on_candle: Callable[[CompletedCandle], Awaitable[None]],
) -> int:
    """Replay ordered completed candles without wall-clock delays or broker access."""
    count = 0
    for candle in sorted(candles, key=lambda item: (item.opened_at, item.instrument_token)):
        await on_candle(candle)
        count += 1
    return count
