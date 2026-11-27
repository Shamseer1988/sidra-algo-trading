"""Sweep promotion never touches a live strategy, and never skips the record.

Before this, ``promote_sweep_combination`` wrote ``paper_strategies`` directly:
one click on a research screen changed the rules of the strategy trading real
money, with no confirmation, no reason and no row in ``setting_revisions``.

The claims that matter:

* A LIVE strategy is refused, and nothing is written -- not the strategy, not a
  revision, not an audit row, not the sweep's promoted marker.
* An Off or Paper strategy is changed through the one strategy write path, so
  the change is versioned, revisioned and audited like any other.
* A promotion changes rules and never the mode, whatever a stored combination
  happens to carry.
* Only parameters a sweep can vary are written.
* Only an administrator may promote.
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.routes import backtesting as routes
from app.db.models import ApplicationSetting, AuditLog, BacktestSweep, SettingRevision, User, UserRole
from app.db.session import SessionLocal
from app.services import user_admin
from app.services.strategy_registry import STRATEGIES_KEY, StrategyConfiguration


def _config(name: str, **extra) -> StrategyConfiguration:
    return StrategyConfiguration(id=f"t-{uuid4().hex[:8]}", name=name, strategy_type="rs-pullback-v1", **extra)


@pytest.fixture
async def admin():
    """An administrator, and the strategy list put back exactly as it was."""
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        saved = row.value if row is not None else None
        email = f"promote-{uuid4().hex[:10]}@test.invalid"
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
        await session.execute(delete(BacktestSweep).where(BacktestSweep.created_by_user_id == user.id))
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


async def _sweep(user: User, strategy_id: str, parameters: dict) -> BacktestSweep:
    async with SessionLocal() as session:
        sweep = BacktestSweep(
            created_by_user_id=user.id,
            status="COMPLETED",
            strategy_id=strategy_id,
            strategy_snapshot={},
            start_date=date(2026, 10, 1),
            end_date=date(2026, 11, 1),
            timeframe_seconds=60,
            instrument_tokens=["NSE_EQ|TEST"],
            parameter_grid={key: [value] for key, value in parameters.items()},
            combination_count=1,
            combinations=[{"index": 0, "parameters": parameters, "in_sample": {}, "validation": {}, "proven": False}],
            best_index=0,
        )
        session.add(sweep)
        await session.commit()
        await session.refresh(sweep)
        return sweep


async def _trail(user: User) -> tuple[int, list[AuditLog]]:
    async with SessionLocal() as session:
        revisions = (
            await session.scalars(select(SettingRevision).where(SettingRevision.changed_by_user_id == user.id))
        ).all()
        audits = (await session.scalars(select(AuditLog).where(AuditLog.user_id == user.id))).all()
    return len(revisions), list(audits)


async def _promote(sweep: BacktestSweep, user: User, index: int = 0, reason: str | None = None):
    async with SessionLocal() as session:
        return await routes.promote_sweep_combination(sweep.id, session, index, reason, user)


# --- the decision, on its own -------------------------------------------------


def test_the_refusal_is_for_live_only():
    assert routes.promotion_refusal(_config("live", enabled=True, live_orders=True)) == routes.LIVE_PROMOTION_REFUSED
    assert routes.promotion_refusal(_config("paper", enabled=True, live_orders=False)) is None
    assert routes.promotion_refusal(_config("off", enabled=False, live_orders=True)) is None
    assert routes.promotion_refusal(None) is not None


def test_only_sweepable_strategy_fields_can_be_promoted():
    for field in ("enabled", "live_orders", "id", "name", "strategy_type", "universe", "version", "exit_rules"):
        assert field not in routes.PROMOTABLE_FIELDS
    assert "minimum_score" in routes.PROMOTABLE_FIELDS


# --- through the route --------------------------------------------------------


async def test_a_live_strategy_is_refused_and_nothing_is_written(admin):
    live = _config("Live one", enabled=True, live_orders=True, minimum_score=71)
    await _seed(live)
    sweep = await _sweep(admin, live.id, {"minimum_score": 85})

    with pytest.raises(HTTPException) as refused:
        await _promote(sweep, admin, reason="looks better")
    assert refused.value.status_code == 409
    assert "LIVE" in refused.value.detail

    stored = (await _stored())[live.id]
    assert stored.model_dump() == live.model_dump()
    revisions, audits = await _trail(admin)
    assert revisions == 0
    assert not [item for item in audits if item.event_type == "backtest.sweep_promoted"]
    async with SessionLocal() as session:
        assert (await session.get(BacktestSweep, sweep.id)).promoted_index is None


async def test_a_paper_strategy_is_changed_with_a_revision_and_an_audit_row(admin):
    paper = _config("Paper one", enabled=True, live_orders=False, minimum_score=71)
    other = _config("Untouched", enabled=True, live_orders=True)
    await _seed(paper, other)
    sweep = await _sweep(admin, paper.id, {"minimum_score": 85, "minimum_rr": 2.5})

    result = await _promote(sweep, admin, reason="sweep 85 held out of sample")
    promoted = next(item for item in result if item.id == paper.id)
    assert promoted.minimum_score == 85 and promoted.minimum_rr == 2.5
    assert promoted.version == paper.version + 1

    stored = await _stored()
    assert stored[paper.id].minimum_score == 85
    assert stored[paper.id].enabled and not stored[paper.id].live_orders
    assert stored[other.id].model_dump() == other.model_dump()

    revisions, audits = await _trail(admin)
    assert revisions == 1
    promoted_audit = [item for item in audits if item.event_type == "backtest.sweep_promoted"]
    assert len(promoted_audit) == 1
    meta = promoted_audit[0].metadata_json
    assert meta["strategy_id"] == paper.id and meta["mode"] == "PAPER"
    assert meta["parameters"] == {"minimum_score": 85, "minimum_rr": 2.5}
    assert meta["reason"] == "sweep 85 held out of sample"
    async with SessionLocal() as session:
        assert (await session.get(BacktestSweep, sweep.id)).promoted_index == 0


async def test_an_off_strategy_can_be_promoted_and_stays_off(admin):
    off = _config("Off one", enabled=False, live_orders=False, minimum_score=71)
    await _seed(off)
    sweep = await _sweep(admin, off.id, {"minimum_score": 80})
    await _promote(sweep, admin)
    stored = (await _stored())[off.id]
    assert stored.minimum_score == 80 and not stored.enabled and not stored.live_orders


async def test_a_combination_cannot_change_the_mode(admin):
    paper = _config("Paper one", enabled=True, live_orders=False, minimum_score=71)
    await _seed(paper)
    sweep = await _sweep(admin, paper.id, {"minimum_score": 80, "live_orders": True, "enabled": True, "id": "x"})
    result = await _promote(sweep, admin)
    assert [item.id for item in result] == [paper.id]
    stored = (await _stored())[paper.id]
    assert stored.minimum_score == 80
    assert stored.enabled and not stored.live_orders


async def test_a_combination_with_only_mode_fields_is_refused(admin):
    paper = _config("Paper one", enabled=True, live_orders=False)
    await _seed(paper)
    sweep = await _sweep(admin, paper.id, {"live_orders": True})
    with pytest.raises(HTTPException) as refused:
        await _promote(sweep, admin)
    assert refused.value.status_code == 422
    assert (await _stored())[paper.id].model_dump() == paper.model_dump()


async def test_an_invalid_value_is_refused_without_writing(admin):
    paper = _config("Paper one", enabled=True, live_orders=False, minimum_score=71)
    await _seed(paper)
    sweep = await _sweep(admin, paper.id, {"minimum_score": 500})
    with pytest.raises(HTTPException) as refused:
        await _promote(sweep, admin)
    assert refused.value.status_code == 422
    assert (await _stored())[paper.id].minimum_score == 71
    assert (await _trail(admin))[0] == 0


async def test_a_missing_strategy_or_combination_is_refused(admin):
    paper = _config("Paper one", enabled=True, live_orders=False)
    await _seed(paper)
    gone = await _sweep(admin, "no-such-strategy", {"minimum_score": 80})
    with pytest.raises(HTTPException) as missing:
        await _promote(gone, admin)
    assert missing.value.status_code == 404
    sweep = await _sweep(admin, paper.id, {"minimum_score": 80})
    with pytest.raises(HTTPException) as out_of_range:
        await _promote(sweep, admin, index=7)
    assert out_of_range.value.status_code == 404


async def test_an_unchanged_combination_is_refused(admin):
    paper = _config("Paper one", enabled=True, live_orders=False, minimum_score=80)
    await _seed(paper)
    sweep = await _sweep(admin, paper.id, {"minimum_score": 80})
    with pytest.raises(HTTPException) as refused:
        await _promote(sweep, admin)
    assert refused.value.status_code == 409
    assert (await _trail(admin))[0] == 0


async def test_only_an_administrator_may_promote():
    """The role check is a FastAPI dependency, so it is checked on the route itself."""
    route = next(
        item for item in routes.router.routes if getattr(item, "path", "") == "/backtests/sweeps/{sweep_id}/promote"
    )
    user_dependency = next(dep for dep in route.dependant.dependencies if getattr(dep, "name", None) == "user")
    trader = User(id=uuid4(), email="t@test.invalid", password_hash="x", role=UserRole.TRADER, is_active=True)
    with pytest.raises(HTTPException) as refused:
        await user_dependency.call(trader)
    assert refused.value.status_code == 403
