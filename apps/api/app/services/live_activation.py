"""Arm and disarm live submission.

Configuration decides whether live trading is *possible*. This decides whether
it is armed *right now*, and the difference is the whole point: a flag set once
stays set, so a system armed on a Monday morning is still armed on a Friday
evening when nobody is watching it. An activation carries an expiry, which makes
"off" the state the system returns to on its own rather than the state someone
has to remember to restore.

Activation is recorded with the readiness report as it stood at the time, so a
later review can see what the administrator was actually told when they armed
it, rather than what the gates happen to say afterwards.

Nothing here places an order, and activation alone authorises nothing: it is one
gate among several in ``live_execution``.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveActivation, User
from app.services.live_readiness import LiveReadinessReport


class LiveActivationError(RuntimeError):
    """Activation was refused. The message is safe to show an operator."""


async def activate_live_trading(
    session: AsyncSession,
    settings: Settings,
    report: LiveReadinessReport,
    user: User,
    *,
    reason: str,
) -> LiveActivation:
    """Arm live submission for a bounded window.

    Refuses unless every readiness gate passes. Activation is not a way to
    override the gates — it is the last of them, and an activation granted over
    a failing gate would make the whole report advisory.
    """
    if not report.ready_for_activation:
        blocked = ", ".join(report.blocking_activation)
        raise LiveActivationError(f"Live readiness gates are not satisfied: {blocked}")

    now = datetime.now(UTC)
    record = LiveActivation(
        activated_by_user_id=user.id,
        reason=reason[:255],
        gate_snapshot=report.snapshot(),
        expires_at=now + timedelta(minutes=settings.live_activation_ttl_minutes),
    )
    session.add(record)
    await session.flush()
    return record


async def revoke_live_activation(session: AsyncSession, *, reason: str) -> LiveActivation | None:
    """Disarm immediately.

    Revocation never fails for lack of an activation: an operator reaching for
    this is trying to make the system stop, and answering "there was nothing to
    stop" with an error would be the wrong response to that intent.
    """
    record = await session.scalar(select(LiveActivation).order_by(LiveActivation.created_at.desc()).limit(1))
    if record is None or record.revoked_at is not None:
        return record
    record.revoked_at = datetime.now(UTC)
    record.revoked_reason = reason[:255]
    await session.flush()
    return record


async def current_activation(session: AsyncSession) -> LiveActivation | None:
    """The activation in force, or None. Expiry and revocation both mean None."""
    record = await session.scalar(select(LiveActivation).order_by(LiveActivation.created_at.desc()).limit(1))
    if record is None or record.revoked_at is not None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return record if expires_at > datetime.now(UTC) else None
