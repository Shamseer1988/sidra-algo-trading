"""The three indicator endpoints, exercised the way the settings screen uses them.

The behaviour worth pinning here is `source`. It is the only thing on that
screen an operator cannot work out from the numbers themselves: before the
first save the deployment is running on its `.env`, and after it the file is
never read for these values again. If `source` ever lied, somebody would edit
`.env`, restart, see nothing change, and spend an afternoon on it.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.api.routes.settings import (
    get_indicator_settings,
    indicator_catalog,
    update_indicator_settings,
)
from app.core.config import get_settings
from app.db.models import ApplicationSetting, AuditLog, SettingRevision
from app.db.session import SessionLocal
from app.services.indicator_settings import INDICATOR_KEY, IndicatorSettings, from_environment

OPERATOR = SimpleNamespace(id=None)


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(SettingRevision).where(SettingRevision.key == INDICATOR_KEY))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == INDICATOR_KEY))
        await session.execute(delete(AuditLog).where(AuditLog.event_type == "settings.indicators_updated"))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


def settings():
    return get_settings()


async def test_the_catalogue_says_environment_before_anything_is_saved():
    async with SessionLocal() as session:
        catalog = await indicator_catalog(OPERATOR, session, settings())
    assert catalog.source == "ENVIRONMENT"


async def test_the_catalogue_says_database_after_a_save():
    async with SessionLocal() as session:
        await update_indicator_settings(from_environment(settings()), session, settings(), OPERATOR)
    async with SessionLocal() as session:
        catalog = await indicator_catalog(OPERATOR, session, settings())
    assert catalog.source == "DATABASE"


async def test_every_stored_field_is_described():
    async with SessionLocal() as session:
        catalog = await indicator_catalog(OPERATOR, session, settings())
    assert {item.key for item in catalog.settings} == set(IndicatorSettings.model_fields)


async def test_the_catalogue_carries_the_live_values():
    saved = from_environment(settings()).model_copy(update={"atr_period": 21})
    async with SessionLocal() as session:
        await update_indicator_settings(saved, session, settings(), OPERATOR)
    async with SessionLocal() as session:
        catalog = await indicator_catalog(OPERATOR, session, settings())
    assert next(item for item in catalog.settings if item.key == "atr_period").value == 21


async def test_a_save_is_versioned_and_audited():
    saved = from_environment(settings()).model_copy(update={"ema_fast_period": 8})
    async with SessionLocal() as session:
        await update_indicator_settings(saved, session, settings(), OPERATOR)
    async with SessionLocal() as session:
        revisions = list(
            (
                await session.scalars(SettingRevision.__table__.select().where(SettingRevision.key == INDICATOR_KEY))
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    AuditLog.__table__.select().where(AuditLog.event_type == "settings.indicators_updated")
                )
            ).all()
        )
    assert len(revisions) == 1
    assert len(events) == 1


async def test_the_first_save_records_that_the_values_used_to_come_from_env():
    async with SessionLocal() as session:
        await update_indicator_settings(from_environment(settings()), session, settings(), OPERATOR)
    async with SessionLocal() as session:
        event = (
            await session.execute(
                AuditLog.__table__.select().where(AuditLog.event_type == "settings.indicators_updated")
            )
        ).first()
    assert event.metadata_json["previous_source"] == "ENVIRONMENT"


async def test_a_later_save_records_that_they_already_came_from_the_database():
    async with SessionLocal() as session:
        await update_indicator_settings(from_environment(settings()), session, settings(), OPERATOR)
    async with SessionLocal() as session:
        await update_indicator_settings(
            from_environment(settings()).model_copy(update={"atr_period": 20}), session, settings(), OPERATOR
        )
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    AuditLog.__table__.select()
                    .where(AuditLog.event_type == "settings.indicators_updated")
                    .order_by(AuditLog.created_at.asc())
                )
            ).all()
        )
    assert [row.metadata_json["previous_source"] for row in rows] == ["ENVIRONMENT", "DATABASE"]


async def test_the_plain_read_agrees_with_what_was_saved():
    saved = from_environment(settings()).model_copy(update={"opening_range_minutes": 30})
    async with SessionLocal() as session:
        await update_indicator_settings(saved, session, settings(), OPERATOR)
    async with SessionLocal() as session:
        assert (await get_indicator_settings(OPERATOR, session, settings())).opening_range_minutes == 30
