"""Open the live trading session on a schedule, or refuse to and say why.

Arming used to be a deliberate human act: an administrator ran a reconcile,
read it, and armed with a typed reason. This module does that unattended, which
removes the one step where a person looked at the broker's view of the account
before real money started moving.

So the sequence is written to **abort rather than improvise**. Every step has
exactly one success condition; anything else stops the run, leaves the system
disarmed, and sends a Telegram message naming the step that stopped it. There
is no retry, no partial arm and no "arm anyway" path, because a scheduled job
that recovers from a condition it does not understand is how an account ends up
trading against a broker state nobody checked.

Ordering matters and is not arbitrary:

* The reconcile must pass **before** the arm, because ``activate_live_trading``
  reads a reconciliation that is at most ``RECONCILIATION_FRESHNESS`` old. Doing
  it the other way round would arm against a stale verdict.
* The scanner starts **last**. If arming fails the scanner never runs, so a
  failed open is a quiet day rather than a day of signals that cannot be filled.
* ``application_mode`` and ``live_trading_enabled`` are checked first and
  together. A paper deployment running this code must do nothing at all, and
  must not discover that by failing later on a missing broker.

The job is idempotent within a day: arming while already armed is refused by
``activate_live_trading``, and the run reports that as a normal outcome rather
than an error.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services.auth import hash_password
from app.services.live_activation import LiveActivationError, activate_live_trading, current_activation
from app.services.live_execution_gateway import BrokerNotSelectedError, live_report_adapter
from app.services.live_readiness import inspect_live_readiness
from app.services.live_reconciliation import persist_live_reconciliation, reconcile_live_execution
from app.services.safety import SCANNER_CONTROL_KEY
from app.services.trading_calendar import MARKET_TIMEZONE, TradingCalendar

logger = structlog.get_logger("live_session_open")

# The scheduler acts as this account so that an audit row can always answer
# "who armed this". It is created inactive and with a password nobody holds:
# deps.py and the login route both refuse an inactive user, so this identity
# exists for attribution and cannot be signed in as.
AUTOMATION_EMAIL = "automation@sidra.local"


@dataclass
class OpenResult:
    """What the run did, in terms the Telegram message and the tests both use."""

    opened: bool
    step: str
    detail: str
    expires_at: datetime | None = None
    findings: list[str] = field(default_factory=list)


async def automation_user(session: AsyncSession) -> User:
    """Return the automation identity, creating it once if absent.

    Provisioned here rather than in a migration or a setup step because a
    deployment that forgets it would fail at 08:45 on a trading morning, which
    is the worst possible time to discover a missing row.
    """
    user = await session.scalar(select(User).where(User.email == AUTOMATION_EMAIL))
    if user is not None:
        return user
    user = User(
        email=AUTOMATION_EMAIL,
        # Hashed from a value that is generated here and never returned,
        # stored or logged. Combined with is_active=False this account has no
        # route to a session.
        password_hash=hash_password(secrets.token_urlsafe(64)),
        role=UserRole.ADMIN,
        is_active=False,
    )
    session.add(user)
    await session.flush()
    logger.info("live_session_open.automation_user_created", email=AUTOMATION_EMAIL)
    return user


async def _start_scanner(settings: Settings, session: AsyncSession, user: User) -> None:
    from redis.asyncio import Redis

    from app.services.safety import emergency_stop_state

    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        if (await emergency_stop_state(redis)).get("active") == "true":
            raise RuntimeError("Emergency stop is active")
        await redis.set(SCANNER_CONTROL_KEY, "RUNNING")
    finally:
        await redis.aclose()
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="scanner.running",
            metadata_json={"requested_at": datetime.now(UTC).isoformat(), "source": "scheduled_open"},
        )
    )


async def open_live_session(settings: Settings, calendar: TradingCalendar, *, dry_run: bool = False) -> OpenResult:
    """Run the open sequence. Never raises; every failure is a returned result.

    ``dry_run`` stops immediately after the reconcile, before anything is armed
    or started. It deliberately shares this function rather than living in a
    script of its own: a rehearsal that exercises different code from the real
    thing proves nothing about the real thing.
    """
    now = datetime.now(UTC)
    today = now.astimezone(MARKET_TIMEZONE).date()

    status = calendar.status_at(now)
    if not status.trading_day:
        return OpenResult(False, "calendar", f"Not a trading day: {status.reason}")

    # Checked together and first: these two are what separate a paper
    # deployment from one that can move money, and neither is meaningful alone.
    if settings.application_mode != "LIVE" or not settings.live_trading_enabled:
        return OpenResult(
            False,
            "runtime",
            f"Runtime is {settings.application_mode} with LIVE_TRADING_ENABLED="
            f"{str(settings.live_trading_enabled).lower()}; nothing to open.",
        )

    async with SessionLocal() as session:
        user = await automation_user(session)
        await session.commit()

        existing = await current_activation(session)
        if existing is not None:
            return OpenResult(
                True,
                "already_armed",
                "Already armed; leaving the existing window alone.",
                expires_at=existing.expires_at,
            )

        # Reconcile. A broker that cannot be reached is a blocked verdict, not
        # an exception to route around: being unable to check is itself a
        # reason not to trade.
        try:
            adapter = await live_report_adapter(settings, session)
        except BrokerNotSelectedError as exc:
            return OpenResult(False, "broker", f"No live broker selected: {exc}")
        except Exception as exc:
            return OpenResult(False, "broker", f"Broker login failed: {type(exc).__name__}: {exc}")

        try:
            report = await reconcile_live_execution(session, adapter)
            record = await persist_live_reconciliation(session, report)
            session.add(
                AuditLog(
                    user_id=user.id,
                    event_type="live.reconciled",
                    metadata_json={"source": "scheduled_open", "safe_to_trade": record.safe_to_trade},
                )
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            return OpenResult(False, "reconcile", f"Reconciliation failed: {type(exc).__name__}: {exc}")

        if not record.safe_to_trade:
            findings = [item.get("detail", "") for item in (record.findings or [])]
            return OpenResult(False, "reconcile", record.detail, findings=findings)

        if dry_run:
            return OpenResult(
                False,
                "dry_run",
                "Reconcile passed. Stopping before the arm (dry run).",
            )

        # Arm. activate_live_trading re-checks every gate, so a gate that
        # changed between the reconcile and here still refuses.
        readiness = await inspect_live_readiness(session, settings)
        try:
            activation = await activate_live_trading(
                session,
                settings,
                readiness,
                user,
                reason=f"Scheduled live session {today.isoformat()}",
            )
        except LiveActivationError as exc:
            await session.rollback()
            return OpenResult(False, "arm", str(exc), findings=readiness.blocking_activation)

        session.add(
            AuditLog(
                user_id=user.id,
                event_type="live.activated",
                metadata_json={
                    "reason": activation.reason,
                    "expires_at": activation.expires_at.isoformat(),
                    "source": "scheduled_open",
                },
            )
        )
        await session.commit()
        await session.refresh(activation)
        expires_at = activation.expires_at

        # Last, so that a failed arm never leaves a scanner producing signals
        # that cannot be filled.
        try:
            await _start_scanner(settings, session, user)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            return OpenResult(
                False,
                "scanner",
                f"Armed, but the scanner did not start: {type(exc).__name__}: {exc}",
                expires_at=expires_at,
            )

    return OpenResult(True, "opened", "Reconciled, armed and scanning.", expires_at=expires_at)
