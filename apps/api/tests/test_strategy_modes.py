"""U1: each strategy's mode -- Off, Paper or Live -- changed from the UI, one at a time.

The claims that matter: going live needs an explicit confirmation; the general
"Save strategies" can never change whether a strategy runs or sends real
orders; a new strategy always starts off and on paper; and each mode change is
one recorded change. Swing strategies take Off or Paper, never Live.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.routes import swing as swing_routes
from app.api.routes.settings import (
    StrategyModeRequest,
    set_strategy_mode,
    strategy_mode,
    update_strategies,
)
from app.db.models import ApplicationSetting, AuditLog, SettingRevision
from app.db.session import SessionLocal
from app.services.strategy_registry import STRATEGIES_KEY, StrategyConfiguration
from app.services.swing_paper import SWING_MODES_KEY, paper_strategies, swing_modes
from app.services.swing_strategies import BASE_BREAKOUT, TREND_PULLBACK

ADMIN = SimpleNamespace(id=None)


def strategy(sid: str, *, enabled: bool, live: bool) -> dict:
    return StrategyConfiguration(
        id=sid, name=f"Strategy {sid}", strategy_type="rs-pullback-v1", enabled=enabled, live_orders=live
    ).model_dump()


@pytest.fixture(autouse=True)
async def stored():
    """Two strategies -- one live, one off -- restored to whatever was there afterwards."""
    async with SessionLocal() as session:
        saved = {key: await session.get(ApplicationSetting, key) for key in (STRATEGIES_KEY, SWING_MODES_KEY)}
        backup = {key: (row.value if row else None) for key, row in saved.items()}
        for key in (STRATEGIES_KEY, SWING_MODES_KEY):
            await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == key))
        session.add(
            ApplicationSetting(
                key=STRATEGIES_KEY,
                value=[strategy("live-one", enabled=True, live=True), strategy("off-one", enabled=False, live=False)],
            )
        )
        await session.commit()
    yield
    async with SessionLocal() as session:
        for key, value in backup.items():
            await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == key))
            if value is not None:
                session.add(ApplicationSetting(key=key, value=value))
        await session.execute(delete(SettingRevision).where(SettingRevision.key.in_((STRATEGIES_KEY, SWING_MODES_KEY))))
        await session.commit()


def by_id(items) -> dict:  # noqa: ANN001
    return {item.id: item for item in items}


async def set_mode(sid: str, mode: str, *, confirm: bool = False, reason: str | None = None):
    async with SessionLocal() as session:
        return by_id(
            await set_strategy_mode(
                sid, StrategyModeRequest(mode=mode, confirm_live=confirm, reason=reason), session, ADMIN
            )
        )


# --- intraday strategies -------------------------------------------------------------


async def test_off_paper_and_live_map_to_the_two_stored_flags():
    assert strategy_mode((await set_mode("off-one", "PAPER"))["off-one"]) == "PAPER"
    assert strategy_mode((await set_mode("off-one", "LIVE", confirm=True))["off-one"]) == "LIVE"
    assert strategy_mode((await set_mode("off-one", "OFF"))["off-one"]) == "OFF"


async def test_going_live_without_confirmation_is_refused():
    with pytest.raises(HTTPException) as refused:
        await set_mode("off-one", "LIVE")
    assert refused.value.status_code == 422
    assert "real money" in refused.value.detail
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
    assert {item["id"]: item["live_orders"] for item in row.value}["off-one"] is False


async def test_an_unknown_strategy_or_mode_is_refused():
    with pytest.raises(HTTPException) as missing:
        await set_mode("nope", "PAPER")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as bad:
        await set_mode("off-one", "SOMETIMES")
    assert bad.value.status_code == 422


async def test_a_mode_change_moves_only_that_strategy_and_is_one_recorded_change():
    await set_mode("off-one", "PAPER", reason="forward test")
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        flags = {item["id"]: (item["enabled"], item["live_orders"]) for item in row.value}
        revision = await session.scalar(
            select(SettingRevision)
            .where(SettingRevision.key == STRATEGIES_KEY)
            .order_by(SettingRevision.created_at.desc())
            .limit(1)
        )
        audit = await session.scalar(
            select(AuditLog)
            .where(AuditLog.event_type == "settings.strategy_mode_changed")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    assert flags == {"live-one": (True, True), "off-one": (True, False)}
    assert revision.changed_keys == ["off-one.enabled"]
    assert audit.metadata_json["from"] == "OFF" and audit.metadata_json["to"] == "PAPER"
    assert audit.metadata_json["reason"] == "forward test"


async def test_the_general_save_cannot_change_whether_a_strategy_runs_or_trades_live():
    async with SessionLocal() as session:
        items = [
            StrategyConfiguration.model_validate(v)
            for v in (await session.get(ApplicationSetting, STRATEGIES_KEY)).value
        ]
        tampered = [
            item.model_copy(
                update={"enabled": not item.enabled, "live_orders": not item.live_orders, "minimum_score": 70}
            )
            for item in items
        ]
        saved = by_id(await update_strategies(tampered, session, ADMIN))
    assert (saved["live-one"].enabled, saved["live-one"].live_orders) == (True, True)
    assert (saved["off-one"].enabled, saved["off-one"].live_orders) == (False, False)
    assert saved["off-one"].minimum_score == 70  # everything else still saves


async def test_a_new_strategy_always_starts_off_and_on_paper():
    """The Add button used to copy the first strategy -- live flag included."""
    async with SessionLocal() as session:
        items = [
            StrategyConfiguration.model_validate(v)
            for v in (await session.get(ApplicationSetting, STRATEGIES_KEY)).value
        ]
        copy = items[0].model_copy(update={"id": "brand-new", "name": "Copied from the live one"})
        assert copy.live_orders and copy.enabled
        saved = by_id(await update_strategies(items + [copy], session, ADMIN))
    assert (saved["brand-new"].enabled, saved["brand-new"].live_orders) == (False, False)


# --- swing strategies ------------------------------------------------------------------


async def test_swing_defaults_are_trend_pullback_on_paper_and_breakout_off():
    async with SessionLocal() as session:
        assert await swing_modes(session) == {TREND_PULLBACK: "PAPER", BASE_BREAKOUT: "OFF"}
        assert await paper_strategies(session) == (TREND_PULLBACK,)


async def test_a_swing_strategy_switches_between_off_and_paper_and_the_desk_follows():
    async with SessionLocal() as session:
        views = await swing_routes.set_swing_strategy_mode(
            BASE_BREAKOUT, swing_routes.SwingModeRequest(mode="PAPER"), session, ADMIN
        )
        assert {v.id: v.mode for v in views}[BASE_BREAKOUT] == "PAPER"
        await swing_routes.set_swing_strategy_mode(
            TREND_PULLBACK, swing_routes.SwingModeRequest(mode="OFF"), session, ADMIN
        )
        assert await paper_strategies(session) == (BASE_BREAKOUT,)


async def test_swing_live_is_refused_with_the_reason():
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await swing_routes.set_swing_strategy_mode(
                TREND_PULLBACK, swing_routes.SwingModeRequest(mode="LIVE"), session, ADMIN
            )
    assert refused.value.status_code == 422
    assert "no live risk limits" in refused.value.detail
    async with SessionLocal() as session:
        listed = await swing_routes.swing_strategies(None, session)
    assert all(not view.live_available and "not available" in view.live_blocked_reason for view in listed)
    assert all(view.evidence for view in listed)
