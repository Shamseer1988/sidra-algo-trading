"""When the ranking is built, and who decides.

The time lived in ``.env`` only. It defaulted to 09:25 while the first candle
of the day closes at 09:15, so on 12 October the scanner's first look found an
empty ranking -- and six instruments the ATR band had rejected were scanned all
day. Moving the time onto the settings screen is only half of it: a screen that
lets the ranking be scheduled after entries begin reproduces the same failure
with more clicks, so the pair is checked when it is saved.
"""

from types import SimpleNamespace

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls, ranking_built_too_late
from app.services import universe
from app.services.settings_catalog import SPECS_BY_KEY

pytestmark = pytest.mark.anyio


def controls(**overrides) -> TradingControls:
    return TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, **overrides})


def test_the_default_leaves_room_before_the_default_trade_start() -> None:
    """The two defaults have to be consistent with each other, or a deployment
    that changes nothing is already in the broken configuration."""
    saved = controls()
    assert saved.universe_refresh_time == "09:16"
    assert saved.universe_refresh_time < saved.trade_start_time


def test_it_is_on_the_settings_screen() -> None:
    """The point of the change: an ordinary trading decision must not need a
    .env edit and a container restart."""
    spec = SPECS_BY_KEY["universe_refresh_time"]
    assert spec.unit == "TIME_IST"
    described = spec.describe(TradingControls.model_fields, "09:16", None)
    assert described["value"] == "09:16"
    assert described["kind"] == "time"


def test_a_ranking_scheduled_after_entries_begin_is_refused() -> None:
    refusal = ranking_built_too_late(
        controls(universe_enabled=True, universe_refresh_time="09:30", trade_start_time="09:24")
    )
    assert refusal is not None
    assert "09:30" in refusal and "09:24" in refusal
    assert "cannot filter" in refusal


def test_the_same_minute_is_refused_because_it_has_no_order() -> None:
    assert ranking_built_too_late(
        controls(universe_enabled=True, universe_refresh_time="09:24", trade_start_time="09:24")
    )


def test_a_ranking_built_before_the_start_is_accepted() -> None:
    assert (
        ranking_built_too_late(controls(universe_enabled=True, universe_refresh_time="09:16", trade_start_time="09:30"))
        is None
    )


def test_the_pair_is_not_checked_while_the_ranking_is_off() -> None:
    """Refusing an unrelated change because of a time that governs nothing
    would be a worse screen than the bug."""
    assert (
        ranking_built_too_late(
            controls(universe_enabled=False, universe_refresh_time="09:30", trade_start_time="09:24")
        )
        is None
    )


def test_a_contradicting_configuration_can_still_be_read_back() -> None:
    """Why the check is on the write path and not a model validator.

    A model validator refuses to *load* what is already stored. A deployment
    whose trade start sits at or before the refresh time -- which the new
    default can create without anybody touching it -- would then come up to a
    settings screen that 500s, with no way in to correct the value. Reading
    must always work; saving is where the invariant belongs.
    """
    stored = {**DEFAULT_TRADING_CONTROLS, "universe_enabled": True, "trade_start_time": "09:15"}
    loaded = TradingControls.model_validate(stored)
    assert loaded.trade_start_time == "09:15"
    assert loaded.universe_refresh_time == "09:16"
    assert ranking_built_too_late(loaded) is not None


async def test_the_saved_time_governs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stored():
        return controls(universe_refresh_time="09:20")

    monkeypatch.setattr(universe, "_stored_controls", stored)
    settings = SimpleNamespace(universe_refresh_time="09:25")
    assert await universe.refresh_time(settings) == "09:20"  # type: ignore[arg-type]


async def test_an_unreadable_settings_row_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_stored_controls`` answers None when the database cannot be read. A
    ranking that refuses to build then is a scanner filtering against nothing,
    which is the failure this whole change is about."""

    async def unreadable():
        return None

    monkeypatch.setattr(universe, "_stored_controls", unreadable)
    settings = SimpleNamespace(universe_refresh_time="09:25")
    assert await universe.refresh_time(settings) == "09:25"  # type: ignore[arg-type]


# --- the write path -------------------------------------------------------


async def test_the_route_refuses_to_save_a_ranking_built_too_late() -> None:
    """Checking it where it is saved is only useful if the route runs the check."""
    from fastapi import HTTPException
    from sqlalchemy import delete

    from app.api.routes.settings import TRADING_KEY, update_trading_controls
    from app.db.models import ApplicationSetting, AuditLog, SettingRevision
    from app.db.session import SessionLocal

    async def clean() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(SettingRevision).where(SettingRevision.key == TRADING_KEY))
            await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
            await session.execute(delete(AuditLog).where(AuditLog.event_type == "settings.trading_updated"))
            await session.commit()

    operator = SimpleNamespace(id=None)
    await clean()
    try:
        async with SessionLocal() as session:
            with pytest.raises(HTTPException) as caught:
                await update_trading_controls(
                    controls(universe_enabled=True, universe_refresh_time="09:30", trade_start_time="09:24"),
                    session,
                    operator,
                )
        assert caught.value.status_code == 422
        assert "cannot filter" in str(caught.value.detail)

        # Refused means not stored: a 422 that had already written the row
        # would leave the contradiction in place and only look like a refusal.
        async with SessionLocal() as session:
            assert await session.get(ApplicationSetting, TRADING_KEY) is None

        async with SessionLocal() as session:
            saved = await update_trading_controls(
                controls(universe_enabled=True, universe_refresh_time="09:16", trade_start_time="09:30"),
                session,
                operator,
            )
        assert saved.universe_refresh_time == "09:16"
    finally:
        await clean()
