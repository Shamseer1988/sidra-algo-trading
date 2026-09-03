"""Composite market-regime snapshot: intraday NIFTY structure + India VIX + breadth.

The regime is advisory unless ``MARKET_REGIME_ENABLED`` is set, in which case the scanner
uses ``allow_long`` / ``allow_short`` / ``size_multiplier`` from the stored snapshot.
"""

import json
from datetime import UTC, date, datetime

from redis.asyncio import Redis

from app.core.config import Settings
from app.services.market_calculations import compose_market_regime, india_vix_state, market_breadth

MARKET_REGIME_KEY = "market:regime"
BREADTH_KEY_PREFIX = "market:breadth:"
REGIME_TTL_SECONDS = 60 * 60 * 18


def _breadth_key(session_date: date) -> str:
    return f"{BREADTH_KEY_PREFIX}{session_date.isoformat()}"


async def update_breadth_marker(redis: Redis, session_date: date, instrument_token: str, above_vwap: bool) -> None:
    key = _breadth_key(session_date)
    await redis.hset(key, instrument_token, "1" if above_vwap else "0")
    await redis.expire(key, REGIME_TTL_SECONDS)


async def _vix_level(redis: Redis, settings: Settings) -> float | None:
    raw = await redis.get(f"market:tick:{settings.upstox_india_vix_key}")
    if not raw:
        return None
    try:
        value = float(json.loads(raw)["price"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


async def compute_and_store_regime(
    redis: Redis,
    settings: Settings,
    session_date: date,
    benchmark_values: dict,
    vix_prior_close: float | None,
) -> dict:
    raw_breadth = await redis.hgetall(_breadth_key(session_date))
    above = sum(1 for value in raw_breadth.values() if value == "1")
    breadth = market_breadth(above, len(raw_breadth))

    vix = india_vix_state(
        await _vix_level(redis, settings),
        vix_prior_close,
        calm_below=settings.india_vix_calm_below,
        stressed_above=settings.india_vix_stressed_above,
        extreme_above=settings.india_vix_extreme_above,
    )

    regime_name = (benchmark_values.get("nifty_regime") or {}).get("regime")
    prior_close = (benchmark_values.get("prior_day") or {}).get("close")
    last_close = benchmark_values.get("last_close")
    nifty_vs_prior = (
        round((last_close - prior_close) / prior_close * 100, 4)
        if isinstance(prior_close, int | float) and prior_close and isinstance(last_close, int | float)
        else None
    )

    regime = compose_market_regime(regime_name, nifty_vs_prior, vix, breadth)
    regime["session_date"] = session_date.isoformat()
    regime["computed_at"] = datetime.now(UTC).isoformat()
    await redis.set(MARKET_REGIME_KEY, json.dumps(regime), ex=REGIME_TTL_SECONDS)
    return regime
