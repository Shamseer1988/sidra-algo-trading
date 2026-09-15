"""Arming and disarming live submission.

The property worth testing is that "off" is where the system returns on its own.
A flag someone sets stays set; an activation lapses, so a system armed for one
session is not still armed during the next one when nobody is watching.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveActivation
from app.db.session import SessionLocal
from app.services.live_activation import (
    LiveActivationError,
    activate_live_trading,
    current_activation,
    revoke_live_activation,
)


def report(*, ready_for_activation: bool = True, blocking: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ready_for_activation=ready_for_activation,
        blocking_activation=blocking or [],
        gates=[],
        snapshot=lambda: {"status": "READY" if ready_for_activation else "HARD_LOCKED"},
    )


def settings(ttl_minutes: int = 480) -> SimpleNamespace:
    return SimpleNamespace(live_activation_ttl_minutes=ttl_minutes)


def user() -> SimpleNamespace:
    return SimpleNamespace(id=None, email="operator@example.com")


async def clean(session) -> None:  # noqa: ANN001
    await session.execute(delete(LiveActivation))
    await session.commit()


async def test_arming_requires_every_other_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activation is the last gate, not a way around the others."""
    async with SessionLocal() as session:
        await clean(session)
        with pytest.raises(LiveActivationError, match="external_reconciliation"):
            await activate_live_trading(
                session,
                settings(),
                report(ready_for_activation=False, blocking=["external_reconciliation"]),
                user(),
                reason="going live",
            )
        assert await current_activation(session) is None


async def test_arming_records_who_why_and_what_they_were_told() -> None:
    async with SessionLocal() as session:
        await clean(session)
        record = await activate_live_trading(
            session, settings(), report(), user(), reason="first live session, 10k capital"
        )
        await session.commit()
        assert record.reason == "first live session, 10k capital"
        assert record.gate_snapshot["status"] == "READY"
        assert await current_activation(session) is not None
        await clean(session)


async def test_an_activation_lapses_without_anyone_acting() -> None:
    """The default state of the system at a later moment is off."""
    async with SessionLocal() as session:
        await clean(session)
        session.add(
            LiveActivation(
                reason="stale",
                gate_snapshot={},
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()
        assert await current_activation(session) is None
        await clean(session)


async def test_revocation_takes_effect_immediately() -> None:
    async with SessionLocal() as session:
        await clean(session)
        await activate_live_trading(session, settings(), report(), user(), reason="live session")
        await session.commit()
        await revoke_live_activation(session, reason="stopping for the day")
        await session.commit()
        assert await current_activation(session) is None
        await clean(session)


async def test_revoking_nothing_is_not_an_error() -> None:
    """Someone reaching for this wants the system to stop, not a lecture."""
    async with SessionLocal() as session:
        await clean(session)
        assert await revoke_live_activation(session, reason="just in case") is None


async def test_a_second_revocation_is_harmless() -> None:
    async with SessionLocal() as session:
        await clean(session)
        await activate_live_trading(session, settings(), report(), user(), reason="live session")
        await session.commit()
        await revoke_live_activation(session, reason="first")
        await session.commit()
        record = await revoke_live_activation(session, reason="second")
        await session.commit()
        assert record.revoked_reason == "first"
        await clean(session)


async def test_the_ttl_comes_from_settings() -> None:
    async with SessionLocal() as session:
        await clean(session)
        record = await activate_live_trading(session, settings(ttl_minutes=30), report(), user(), reason="short")
        await session.commit()
        remaining = record.expires_at - datetime.now(UTC)
        assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)
        await clean(session)


async def test_only_the_most_recent_activation_counts() -> None:
    """A revoked activation must not be resurrected by an older armed one."""
    async with SessionLocal() as session:
        await clean(session)
        old = LiveActivation(
            id=uuid4(),
            reason="older",
            gate_snapshot={},
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
        session.add(old)
        await session.commit()
        await activate_live_trading(session, settings(), report(), user(), reason="newer")
        await session.commit()
        await revoke_live_activation(session, reason="disarming")
        await session.commit()
        assert await current_activation(session) is None
        await clean(session)
