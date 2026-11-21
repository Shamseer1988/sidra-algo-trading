"""U7: Strategies > Builder -- make, measure and save a strategy, never live.

The claims that matter: a saved strategy is always Off and paper-only whatever
the draft says; a live strategy's rules cannot be changed from the builder; an
edit never changes a strategy's mode; only an Off strategy can be deleted; and
a draft backtest replays the draft as an enabled paper strategy without
saving it.
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.routes import strategy_builder as routes
from app.core.config import get_settings
from app.db.models import ApplicationSetting, AuditLog, SettingRevision, User, UserRole
from app.db.session import SessionLocal
from app.services import user_admin
from app.services.strategy_registry import STRATEGIES_KEY, StrategyConfiguration, StrategyRegistry


def _config(name: str, **extra) -> StrategyConfiguration:
    return StrategyConfiguration(id=f"t-{uuid4().hex[:8]}", name=name, strategy_type="rs-pullback-v1", **extra)


@pytest.fixture
async def admin():
    """An administrator, and the strategy list put back exactly as it was."""
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        saved = row.value if row is not None else None
        email = f"u7-{uuid4().hex[:10]}@test.invalid"
        user = await user_admin.create_user(session, email, "correct horse battery", UserRole.ADMIN)
        await session.commit()
        await session.refresh(user)
    yield user
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        if saved is None and row is not None:
            await session.delete(row)
        elif saved is not None:
            if row is None:
                session.add(ApplicationSetting(key=STRATEGIES_KEY, value=saved))
            else:
                row.value, row.updated_by_user_id = saved, None
        await session.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
        await session.execute(delete(SettingRevision).where(SettingRevision.changed_by_user_id == user.id))
        await session.flush()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def _seed(*items: StrategyConfiguration) -> None:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        value = [item.model_dump() for item in items]
        if row is None:
            session.add(ApplicationSetting(key=STRATEGIES_KEY, value=value))
        else:
            row.value = value
        await session.commit()


async def _stored() -> dict[str, StrategyConfiguration]:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        return {item["id"]: StrategyConfiguration.model_validate(item) for item in row.value}


async def test_every_tested_type_is_a_template_with_a_plain_description(admin):
    async with SessionLocal() as session:
        catalog = await routes.catalog(session, admin)
    types = {meta.identifier for meta in StrategyRegistry.metadata()}
    assert {template.strategy_type for template in catalog.templates} == types
    for template in catalog.templates:
        assert template.about, template.strategy_type
        assert not template.defaults.enabled and not template.defaults.live_orders


async def test_a_saved_strategy_is_always_off_and_paper_only(admin):
    live = _config("Live one", enabled=True, live_orders=True)
    await _seed(live)
    draft = _config("Tighter RS", enabled=True, live_orders=True, minimum_score=85)
    async with SessionLocal() as session:
        result = await routes.save_new(routes.SaveRequest(draft=draft, reason="test"), session, admin)
    made = next(item for item in result if item.name == "Tighter RS")
    assert not made.enabled and not made.live_orders
    assert made.id != draft.id and made.minimum_score == 85
    stored = await _stored()
    assert stored[live.id].enabled and stored[live.id].live_orders


async def test_a_duplicate_name_is_refused(admin):
    await _seed(_config("RS Pullback"))
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.save_new(routes.SaveRequest(draft=_config("rs pullback ")), session, admin)
    assert refused.value.status_code == 409


async def test_a_live_strategy_cannot_be_edited_from_the_builder(admin):
    live = _config("Live one", enabled=True, live_orders=True)
    await _seed(live)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.save_existing(
                live.id, routes.SaveRequest(draft=live.model_copy(update={"minimum_score": 10})), session, admin
            )
    assert refused.value.status_code == 409
    assert (await _stored())[live.id].minimum_score == live.minimum_score


async def test_an_edit_changes_the_rules_and_never_the_mode(admin):
    paper = _config("Paper one", enabled=True, live_orders=False)
    await _seed(paper)
    draft = paper.model_copy(update={"minimum_score": 70, "enabled": True, "live_orders": True})
    async with SessionLocal() as session:
        await routes.save_existing(paper.id, routes.SaveRequest(draft=draft), session, admin)
    stored = (await _stored())[paper.id]
    assert stored.minimum_score == 70
    assert stored.enabled and not stored.live_orders
    assert stored.version == paper.version + 1


async def test_only_an_off_strategy_can_be_deleted(admin):
    paper = _config("Paper one", enabled=True, live_orders=False)
    off = _config("Old draft", enabled=False, live_orders=False)
    await _seed(paper, off)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.delete(paper.id, session, admin)
    assert refused.value.status_code == 409
    async with SessionLocal() as session:
        await routes.delete(off.id, session, admin)
    assert set(await _stored()) == {paper.id}


async def test_a_change_is_recorded_as_a_revision_and_an_audit_entry(admin):
    await _seed(_config("Base"))
    async with SessionLocal() as session:
        await routes.save_new(routes.SaveRequest(draft=_config("Audited"), reason="trying a variant"), session, admin)
    async with SessionLocal() as session:
        audit = (
            await session.scalars(
                select(AuditLog).where(AuditLog.user_id == admin.id, AuditLog.event_type == "strategy_builder.created")
            )
        ).all()
    assert audit and audit[0].metadata_json["reason"] == "trying a variant"


async def test_a_draft_backtest_replays_the_draft_as_paper_and_saves_no_strategy(admin, monkeypatch):
    await _seed(_config("Base"))
    before = await _stored()
    seen = {}

    async def fake_execute(request, app_settings, session, user, selected, controls, execution):
        seen["request"], seen["selected"] = request, selected
        return "ran"

    monkeypatch.setattr(routes, "execute_backtest", fake_execute)
    draft = _config("Unsaved idea", enabled=False, live_orders=True)
    async with SessionLocal() as session:
        result = await routes.backtest_draft(
            routes.DraftBacktest(
                draft=draft, start_date=date(2026, 11, 2), end_date=date(2026, 11, 20), instrument_tokens=["NSE_EQ|X"]
            ),
            get_settings(),
            session,
            admin,
        )
    assert result == "ran"
    (replayed,) = seen["selected"]
    assert replayed.enabled and not replayed.live_orders and replayed.name == "Unsaved idea"
    assert seen["request"].strategy_ids == [replayed.id]
    assert await _stored() == before


def test_a_draft_backtest_is_limited_to_32_days():
    with pytest.raises(ValueError):
        routes.DraftBacktest(
            draft=_config("X1x"), start_date=date(2026, 9, 1), end_date=date(2026, 11, 1), instrument_tokens=["A"]
        )
