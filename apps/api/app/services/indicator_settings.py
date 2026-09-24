"""Indicator periods, moved out of the environment file and into the database.

These eight values decide what every indicator means — how long the opening
range is, how many candles an EMA averages, how many sessions a relative-volume
baseline needs. They lived in ``.env``, which made adjusting them a file edit
and a container restart, and made the current value invisible to anyone who was
not on the NAS.

**Environment is the fallback, not the source.** A deployment that has never
saved these through the UI keeps behaving exactly as its ``.env`` says, so this
change is inert until somebody uses it. Once saved, the stored row wins and the
environment variable becomes the value you would fall back to if the row were
deleted. That ordering matters: the reverse would silently override an
operator's saved choice on the next container restart.

**They take effect on the next session, and that is not a limitation.** An EMA
period changed at 11:00 would have one meaning before the change and another
after it, inside one day's data — and the strategy state machine would be
holding a breakout established under the old reading. Worse, the candle
timeframe is what the aggregator buckets ticks into; changing it mid-session
does not reinterpret the day, it corrupts it. So the scanner picks these up at
the session rollover it already performs, and the UI says so.

The one exception to "just a number": ``ema_fast_period`` must stay below
``ema_slow_period``. A fast average slower than the slow one inverts every
trend signal in the system rather than producing an error, which is the kind of
mistake that is only visible in the results.
"""

from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting

INDICATOR_KEY = "indicator_settings"

# Ordered so the catalogue, the API and the fallback all agree on the set.
INDICATOR_FIELDS = (
    "candle_timeframe_seconds",
    "opening_range_minutes",
    "ema_fast_period",
    "ema_slow_period",
    "atr_period",
    "volume_lookback_candles",
    "rvol_baseline_sessions",
    "daily_history_sessions",
)


class IndicatorSettings(BaseModel):
    """The eight periods, with the same bounds the environment schema enforces.

    Restated here rather than imported because these are now two independent
    inputs to the same value — a stored row and an environment variable — and a
    bound that lived in only one of them would let the other through.
    """

    candle_timeframe_seconds: int = Field(default=60, ge=60, le=900)
    opening_range_minutes: int = Field(default=15, ge=5, le=60)
    ema_fast_period: int = Field(default=9, ge=2, le=100)
    ema_slow_period: int = Field(default=21, ge=3, le=200)
    atr_period: int = Field(default=14, ge=2, le=100)
    volume_lookback_candles: int = Field(default=20, ge=3, le=200)
    rvol_baseline_sessions: int = Field(default=10, ge=1, le=60)
    daily_history_sessions: int = Field(default=40, ge=5, le=250)

    @model_validator(mode="after")
    def fast_must_be_faster(self) -> "IndicatorSettings":
        """A fast average slower than the slow one inverts every trend signal.

        It would not raise anywhere; it would just quietly reverse the EMA gate
        and the trend component of every score, which is only visible in the
        results weeks later.
        """
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be lower than ema_slow_period")
        return self


def from_environment(settings: Any) -> IndicatorSettings:
    """What the environment file says, as the fallback."""
    return IndicatorSettings.model_validate(
        {field: getattr(settings, field) for field in INDICATOR_FIELDS if hasattr(settings, field)}
    )


async def load(session: AsyncSession, settings: Any) -> IndicatorSettings:
    """The stored row if there is one, otherwise the environment.

    A stored row that no longer validates — because a bound moved, or somebody
    edited the JSON directly — falls back rather than raising. The scanner
    running on its previous configuration is a better outcome than the scanner
    not running.
    """
    stored = await session.get(ApplicationSetting, INDICATOR_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return from_environment(settings)
    try:
        return IndicatorSettings.model_validate({**from_environment(settings).model_dump(), **stored.value})
    except ValueError:
        return from_environment(settings)


class EffectiveSettings:
    """A settings object with the indicator periods resolved from the database.

    A proxy rather than a copy because ``Settings`` carries secrets and dozens
    of unrelated fields, and the call sites want one object they can pass
    around. Everything not an indicator period is delegated untouched, so this
    cannot accidentally become a second, diverging source for anything else.
    """

    __slots__ = ("_base", "_indicators")

    def __init__(self, base: Any, indicators: IndicatorSettings) -> None:
        self._base = base
        self._indicators = indicators

    def __getattr__(self, name: str) -> Any:
        if name in INDICATOR_FIELDS:
            return getattr(self._indicators, name)
        return getattr(self._base, name)

    @property
    def indicators(self) -> IndicatorSettings:
        return self._indicators

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EffectiveSettings({self._indicators.model_dump()})"


def overlay(settings: Any, indicators: IndicatorSettings) -> EffectiveSettings:
    return EffectiveSettings(settings, indicators)


async def effective(session: AsyncSession, settings: Any) -> EffectiveSettings:
    """The usual entry point: resolve, then overlay."""
    return overlay(settings, await load(session, settings))
