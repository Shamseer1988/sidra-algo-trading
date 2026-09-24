"""Indicator periods, moved from the environment file into the database.

The ordering is the whole test suite in one sentence: **the stored row wins,
and the environment is the fallback**. The reverse would silently overwrite an
operator's saved choice on the next container restart, which is a bug that only
appears days later and looks like the UI not saving.

The rest protects the two properties that make the move safe — a deployment
that has never touched the UI behaves exactly as before, and a stored row that
no longer validates does not stop the scanner.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.db.models import ApplicationSetting
from app.db.session import SessionLocal
from app.services.indicator_settings import (
    INDICATOR_FIELDS,
    INDICATOR_KEY,
    IndicatorSettings,
    effective,
    from_environment,
    load,
    overlay,
)

# Deliberately different from every default, so a test that passes by
# coincidence cannot.
ENVIRONMENT = SimpleNamespace(
    candle_timeframe_seconds=120,
    opening_range_minutes=30,
    ema_fast_period=8,
    ema_slow_period=34,
    atr_period=21,
    volume_lookback_candles=50,
    rvol_baseline_sessions=5,
    daily_history_sessions=90,
    # Something the overlay must pass through untouched.
    nifty_benchmark_token="NSE_INDEX|Nifty 50",
    jwt_secret="not-an-indicator-period",
)


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == INDICATOR_KEY))
        await session.commit()


async def store(**values) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == INDICATOR_KEY))
        session.add(ApplicationSetting(key=INDICATOR_KEY, value=values))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


# --- the ordering that matters -------------------------------------------


async def test_without_a_stored_row_the_environment_is_used_unchanged() -> None:
    """A deployment that has never opened this screen behaves exactly as its
    .env says, so the move is inert until somebody uses it."""
    async with SessionLocal() as session:
        resolved = await load(session, ENVIRONMENT)
    assert resolved.ema_fast_period == 8
    assert resolved.opening_range_minutes == 30
    assert resolved.daily_history_sessions == 90


async def test_a_stored_row_beats_the_environment() -> None:
    """The reverse would overwrite a saved choice on the next restart — a bug
    that surfaces days later and looks like the UI not saving."""
    await store(**{**from_environment(ENVIRONMENT).model_dump(), "ema_fast_period": 5})
    async with SessionLocal() as session:
        resolved = await load(session, ENVIRONMENT)
    assert resolved.ema_fast_period == 5


async def test_a_partial_row_falls_back_per_field_rather_than_wholesale() -> None:
    """A row written before a field existed must not reset the others."""
    await store(ema_fast_period=6)
    async with SessionLocal() as session:
        resolved = await load(session, ENVIRONMENT)
    assert resolved.ema_fast_period == 6
    assert resolved.opening_range_minutes == 30  # still from the environment


# --- failing safe ---------------------------------------------------------


async def test_a_stored_row_that_no_longer_validates_falls_back() -> None:
    """The scanner running on its previous configuration beats the scanner not
    running. A bound can move, or somebody can edit the JSON by hand."""
    await store(**{**from_environment(ENVIRONMENT).model_dump(), "ema_fast_period": 99, "ema_slow_period": 10})
    async with SessionLocal() as session:
        resolved = await load(session, ENVIRONMENT)
    assert resolved.ema_fast_period == 8


async def test_a_row_that_is_not_a_mapping_falls_back() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == INDICATOR_KEY))
        session.add(ApplicationSetting(key=INDICATOR_KEY, value=["nonsense"]))
        await session.commit()
        resolved = await load(session, ENVIRONMENT)
    assert resolved.ema_fast_period == 8


# --- the validator that does not raise anywhere else ---------------------


def test_a_fast_ema_slower_than_the_slow_one_is_refused() -> None:
    """It would not raise in the scoring code; it would quietly reverse the
    trend gate and every trend score, visible only in the results."""
    with pytest.raises(ValueError, match="ema_fast_period must be lower"):
        IndicatorSettings(ema_fast_period=21, ema_slow_period=9)


def test_equal_periods_are_refused_too() -> None:
    with pytest.raises(ValueError, match="ema_fast_period must be lower"):
        IndicatorSettings(ema_fast_period=9, ema_slow_period=9)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candle_timeframe_seconds", 30),
        ("candle_timeframe_seconds", 1800),
        ("opening_range_minutes", 1),
        ("rvol_baseline_sessions", 0),
        ("daily_history_sessions", 1000),
    ],
)
def test_bounds_are_enforced_on_the_stored_shape_too(field: str, value: int) -> None:
    """Two independent inputs to one value. A bound in only one of them lets
    the other through."""
    with pytest.raises(ValueError):
        IndicatorSettings.model_validate({field: value})


# --- the overlay ----------------------------------------------------------


def test_the_overlay_serves_indicator_periods_from_the_resolved_values() -> None:
    resolved = IndicatorSettings(ema_fast_period=7, ema_slow_period=30)
    view = overlay(ENVIRONMENT, resolved)
    assert view.ema_fast_period == 7
    assert view.ema_slow_period == 30


def test_the_overlay_delegates_everything_else_untouched() -> None:
    """A proxy rather than a copy, so it cannot become a second diverging
    source for the dozens of unrelated fields Settings carries."""
    view = overlay(ENVIRONMENT, from_environment(ENVIRONMENT))
    assert view.nifty_benchmark_token == "NSE_INDEX|Nifty 50"
    assert view.jwt_secret == "not-an-indicator-period"


def test_the_overlay_covers_every_indicator_field() -> None:
    """A field added to the model but missing from INDICATOR_FIELDS would read
    from the environment forever, silently ignoring what was saved."""
    view = overlay(SimpleNamespace(), IndicatorSettings())
    for field in IndicatorSettings.model_fields:
        assert field in INDICATOR_FIELDS, field
        assert getattr(view, field) is not None


def test_a_missing_attribute_still_raises_rather_than_returning_none() -> None:
    """A proxy that swallowed typos would turn a crash into a wrong number."""
    view = overlay(ENVIRONMENT, IndicatorSettings())
    with pytest.raises(AttributeError):
        _ = view.not_a_real_setting


async def test_effective_resolves_and_overlays_in_one_step() -> None:
    await store(**{**from_environment(ENVIRONMENT).model_dump(), "atr_period": 30})
    async with SessionLocal() as session:
        view = await effective(session, ENVIRONMENT)
    assert view.atr_period == 30
    assert view.nifty_benchmark_token == "NSE_INDEX|Nifty 50"
