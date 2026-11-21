"""Settings > Swing trading: the paper desk's capital and limits from the screen.

The claims that matter: with nothing saved the desk runs on exactly the values
it always had; a saved set reaches the rules every swing run uses; values out
of range, or that cannot work together, are refused; and a save needs a reason
and is recorded.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.api.routes import swing_settings as routes
from app.db.models import ApplicationSetting, AuditLog, SettingRevision, User, UserRole
from app.db.session import SessionLocal
from app.services import user_admin
from app.services.swing_backtest import SwingRules
from app.services.swing_settings import SWING_RULES_KEY, SwingRuleSettings, swing_rules


def test_the_defaults_are_the_rules_the_desk_already_ran_with():
    assert SwingRuleSettings().rules() == SwingRules()


def test_strategies_pass_through():
    assert SwingRuleSettings().rules(["trend-pullback"]).strategies == ("trend-pullback",)


@pytest.mark.parametrize(
    "values",
    [
        {"capital": 5_000},
        {"risk_per_trade_percent": 0},
        {"max_open_positions": 0},
        {"max_position_percent": 150},
        {"risk_per_trade_percent": 3, "heat_cap_percent": 2},
    ],
)
def test_out_of_range_or_unworkable_values_are_refused(values):
    with pytest.raises(ValidationError):
        SwingRuleSettings(**values)


@pytest.fixture
async def admin():
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, SWING_RULES_KEY)
        saved = row.value if row is not None else None
        user = await user_admin.create_user(
            session, f"sw-{uuid4().hex[:10]}@test.invalid", "correct horse battery", UserRole.ADMIN
        )
        await session.commit()
        await session.refresh(user)
    yield user
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, SWING_RULES_KEY)
        if saved is None and row is not None:
            await session.delete(row)
        elif saved is not None and row is not None:
            row.value, row.updated_by_user_id = saved, None
        await session.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
        await session.execute(delete(SettingRevision).where(SettingRevision.changed_by_user_id == user.id))
        await session.flush()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def test_a_save_reaches_the_rules_and_is_recorded(admin):
    values = SwingRuleSettings(capital=200_000, risk_per_trade_percent=0.5, max_open_positions=4)
    async with SessionLocal() as session:
        view = await routes.update_swing_settings(
            routes.SwingSettingsRequest(values=values, reason="more paper capital"), session, admin
        )
    assert view.saved
    assert {spec.key: spec.value for spec in view.settings}["capital"] == 200_000
    assert view.effective.risk_per_trade == 1_000
    async with SessionLocal() as session:
        rules = await swing_rules(session, ["trend-pullback"])
        entry = await session.scalar(
            select(AuditLog).where(AuditLog.user_id == admin.id, AuditLog.event_type == "settings.swing_rules_updated")
        )
    assert rules.capital == Decimal("200000.0") and rules.max_open_positions == 4
    assert rules.strategies == ("trend-pullback",)
    assert entry.metadata_json["reason"] == "more paper capital" and entry.metadata_json["paper_only"]


async def test_an_unchanged_save_is_refused(admin):
    async with SessionLocal() as session:
        current = await routes.swing_settings(admin, session)
        values = SwingRuleSettings(**{spec.key: spec.value for spec in current.settings})
        with pytest.raises(HTTPException) as refused:
            await routes.update_swing_settings(routes.SwingSettingsRequest(values=values, reason="x"), session, admin)
    assert refused.value.status_code == 422


def test_a_blank_reason_is_refused():
    with pytest.raises(ValidationError):
        routes.SwingSettingsRequest(values=SwingRuleSettings(), reason="")
