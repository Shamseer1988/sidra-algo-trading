"""The settings endpoints a form is built on, exercised as the UI will use them.

Called as functions rather than over HTTP, which is how the rest of this suite
tests routes. What matters here is not the transport but three promises the
backend makes to the screen:

  the form can be generated without the UI knowing any control by name
  a change is versioned and attributable, so "when did this last move" has an answer
  a preset goes through the same validation, versioning and audit as a hand edit

The third is the one worth stating. A preset button that wrote settings by a
shorter path would be a way to raise a risk limit without leaving a trace.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.api.routes.settings import (
    DEFAULT_TRADING_CONTROLS,
    TRADING_KEY,
    ApplyPresetRequest,
    TradingControls,
    apply_preset,
    trading_catalog,
    trading_effective_limits,
    trading_history,
    trading_presets,
    update_trading_controls,
)
from app.db.models import ApplicationSetting, AuditLog, SettingRevision
from app.db.session import SessionLocal
from app.services.risk_profile import CAUTIOUS_PAPER_START, USER_ADVANCED

# The FK is nullable, so a user that never existed is a valid author for a
# revision. Creating a real one would test the fixture, not the route.
OPERATOR = SimpleNamespace(id=None)


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(SettingRevision).where(SettingRevision.key == TRADING_KEY))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.execute(delete(AuditLog).where(AuditLog.event_type == "settings.trading_updated"))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def save(**overrides) -> TradingControls:
    controls = TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, **overrides})
    async with SessionLocal() as session:
        return await update_trading_controls(controls, session, OPERATOR)


# --- the catalogue --------------------------------------------------------


async def test_the_catalogue_describes_every_control_with_its_live_value() -> None:
    """The UI renders from this and hard-codes nothing."""
    await save(minimum_score=72)
    async with SessionLocal() as session:
        catalog = await trading_catalog(None, session)
    by_key = {item.key: item for item in catalog.settings}
    assert set(by_key) == set(TradingControls.model_fields)
    assert by_key["minimum_score"].value == 72
    assert by_key["minimum_score"].unit == "POINTS"
    assert by_key["minimum_score"].maximum == 100


async def test_the_catalogue_carries_the_effective_limits_beside_the_inputs() -> None:
    """So a contradiction between two controls is visible on the same screen
    that creates it."""
    await save(risk_per_trade_percent=1.0, maximum_daily_risk_percent=2.0, maximum_daily_trades=4)
    async with SessionLocal() as session:
        catalog = await trading_catalog(None, session)
    assert catalog.effective.effective_trade_ceiling == 2
    assert catalog.effective.binding_control == "maximum_daily_risk_percent"
    assert any("only 2 of the 4" in warning for warning in catalog.effective.warnings)


async def test_the_catalogue_works_before_anything_has_been_saved() -> None:
    """A fresh install must render a form, not an error."""
    async with SessionLocal() as session:
        catalog = await trading_catalog(None, session)
    assert len(catalog.settings) == len(TradingControls.model_fields)
    assert all(item.last_changed_at is None for item in catalog.settings)


# --- per-control last-changed --------------------------------------------


async def test_only_the_control_that_moved_gets_a_new_timestamp() -> None:
    """The row's updated_at would say the daily stop changed when what
    actually changed was the score — true of the row, false of the field."""
    await save(minimum_score=70)
    async with SessionLocal() as session:
        first = {item.key: item.last_changed_at for item in (await trading_catalog(None, session)).settings}
    assert first["minimum_score"] is not None
    assert first["daily_loss_limit"] is None

    await save(minimum_score=70, daily_loss_limit=500.0)
    async with SessionLocal() as session:
        second = {item.key: item.last_changed_at for item in (await trading_catalog(None, session)).settings}
    assert second["daily_loss_limit"] is not None
    assert second["minimum_score"] == first["minimum_score"]


# --- the history ----------------------------------------------------------


async def test_every_save_is_versioned() -> None:
    await save(minimum_score=70)
    await save(minimum_score=71)
    async with SessionLocal() as session:
        history = await trading_history(None, session)
    assert len(history) == 2
    assert history[0].changed_keys == ["minimum_score"]


async def test_a_loosened_limit_is_named_in_the_history() -> None:
    """Tightening needs no explanation later. Raising a ceiling might."""
    await save(daily_loss_limit=1000.0)
    async with SessionLocal() as session:
        history = await trading_history(None, session)
    assert history[0].risk_increased == ["daily_loss_limit: 400.0 -> 1000.0"]


async def test_the_audit_log_records_the_change_without_relying_on_the_client() -> None:
    """A confirmation the UI could skip is not a confirmation."""
    await save(maximum_daily_trades=8)
    async with SessionLocal() as session:
        entry = await session.scalar(
            AuditLog.__table__.select().where(AuditLog.event_type == "settings.trading_updated")
        )
    assert entry is not None


# --- presets --------------------------------------------------------------


async def test_a_preset_is_offered_with_what_it_would_mean() -> None:
    presets = await trading_presets(None)
    assert {item.key for item in presets} == {CAUTIOUS_PAPER_START, USER_ADVANCED}
    advanced = next(item for item in presets if item.key == USER_ADVANCED)
    assert advanced.effective.daily_loss_percent == "10.00"
    assert advanced.effective.effective_trade_ceiling == 4


async def test_applying_a_preset_writes_it_through_the_same_path() -> None:
    """Versioned and audited exactly as a hand edit is."""
    async with SessionLocal() as session:
        saved = await apply_preset(
            CAUTIOUS_PAPER_START,
            ApplyPresetRequest(preset=CAUTIOUS_PAPER_START),
            session,
            OPERATOR,
        )
    assert saved.daily_loss_limit == 400.0
    async with SessionLocal() as session:
        history = await trading_history(None, session)
    assert len(history) == 1


async def test_a_preset_that_raises_a_limit_needs_an_acknowledgement() -> None:
    """Enforced server-side: a confirmation that lives only in a client can be
    skipped by calling the API."""
    from fastapi import HTTPException

    await save(**DEFAULT_TRADING_CONTROLS)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as raised:
            await apply_preset(USER_ADVANCED, ApplyPresetRequest(preset=USER_ADVANCED), session, OPERATOR)
    assert raised.value.status_code == 409
    assert "daily_loss_limit" in raised.value.detail


async def test_the_acknowledgement_lets_it_through() -> None:
    await save(**DEFAULT_TRADING_CONTROLS)
    async with SessionLocal() as session:
        saved = await apply_preset(
            USER_ADVANCED,
            ApplyPresetRequest(preset=USER_ADVANCED, confirm_risk_increase=True),
            session,
            OPERATOR,
        )
    assert saved.daily_loss_limit == 1000.0
    assert saved.risk_per_trade_percent == 2.5


async def test_a_preset_that_only_tightens_needs_no_acknowledgement() -> None:
    await save(**{**DEFAULT_TRADING_CONTROLS, **{"daily_loss_limit": 5000.0, "risk_per_trade_percent": 4.0}})
    async with SessionLocal() as session:
        saved = await apply_preset(
            CAUTIOUS_PAPER_START,
            ApplyPresetRequest(preset=CAUTIOUS_PAPER_START),
            session,
            OPERATOR,
        )
    assert saved.daily_loss_limit == 400.0


async def test_an_unknown_preset_is_refused_by_name() -> None:
    from fastapi import HTTPException

    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as raised:
            await apply_preset("AGGRESSIVE", ApplyPresetRequest(preset="AGGRESSIVE"), session, OPERATOR)
    assert raised.value.status_code == 404


# --- effective limits -----------------------------------------------------


async def test_the_effective_endpoint_agrees_with_the_catalogue() -> None:
    """Two endpoints serving one truth. Disagreement would mean the warning
    and the form were computed from different readings of the same row."""
    await save(maximum_daily_risk_percent=1.0)
    async with SessionLocal() as session:
        standalone = await trading_effective_limits(None, session)
        catalog = await trading_catalog(None, session)
    assert standalone == catalog.effective
