"""Live-architecture readiness checks that deliberately cannot submit or enable orders.

Each gate reports a fact rather than a wish. Two of them described the system as
it was before the live-execution layer existed — "only the paper risk engine
exists" and "no external-broker reconciliation adapter exists" — and both are now
untrue. A gate that lies about the system is worse than no gate: it is the input
an operator uses to decide whether to go live.

``overall_ready`` is computed from the gates, so a gate added without being
wired into the result refuses rather than passes.

A submission adapter now exists, so the gates are no longer decorative: this
report is what decides whether an administrator may arm live trading, and every
gate below is a condition somebody has to satisfy deliberately. The two that
cannot be satisfied by editing a file are ``external_reconciliation``, which
requires the broker's own state to agree with ours within the last few minutes,
and ``administrator_activation``, which requires a person.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ExecutionReconciliation, LiveReadinessCheck, User

# app.services.live_orders can place, modify and cancel orders at Firstock. This
# flipped in the same change that added it, and the test suite asserts the two
# stay consistent: a True here with no adapter, or an adapter with a False here,
# is a gate that lies to the operator.
SUBMISSION_ADAPTER_IMPLEMENTED = True

# Reconciliation older than this describes an account that may since have moved.
RECONCILIATION_FRESHNESS = timedelta(minutes=15)


@dataclass(frozen=True)
class LiveGate:
    key: str
    label: str
    passed: bool
    detail: str


# Excluded when asking whether a person may arm the system, because requiring it
# there would mean an activation is needed before an activation can be granted.
ACTIVATION_GATE = "administrator_activation"


@dataclass(frozen=True)
class LiveReadinessReport:
    status: str
    overall_ready: bool
    checked_at: datetime
    gates: list[LiveGate]

    @property
    def ready_for_activation(self) -> bool:
        """Every precondition met, so an administrator may now arm the system.

        Distinct from ``overall_ready``, which additionally requires that they
        already have. Submission needs the second; granting an activation needs
        the first, and conflating them is a deadlock.
        """
        return all(gate.passed for gate in self.gates if gate.key != ACTIVATION_GATE)

    @property
    def blocking_activation(self) -> list[str]:
        return [gate.key for gate in self.gates if not gate.passed and gate.key != ACTIVATION_GATE]

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "overall_ready": self.overall_ready,
            "checked_at": self.checked_at.isoformat(),
            "gates": [asdict(gate) for gate in self.gates],
            "ready_for_activation": self.ready_for_activation,
            "broker_submission_permitted": SUBMISSION_ADAPTER_IMPLEMENTED,
        }


async def inspect_live_readiness(session: AsyncSession, settings: Settings) -> LiveReadinessReport:
    database_healthy = False
    try:
        await session.execute(text("SELECT 1"))
        database_healthy = True
    except Exception:
        pass

    redis_healthy = False
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        redis_healthy = bool(await redis.ping())
    except Exception:
        pass
    finally:
        await redis.aclose()

    # Scoped to LIVE deliberately. A clean paper reconciliation says nothing
    # about the broker account, and counting it here is how a gate comes to read
    # green for a system nobody has checked against the broker.
    latest_live_reconciliation = await session.scalar(
        select(ExecutionReconciliation)
        .where(ExecutionReconciliation.mode == "LIVE")
        .order_by(ExecutionReconciliation.created_at.desc())
        .limit(1)
    )
    reconciliation_passed, reconciliation_detail = _reconciliation_gate(latest_live_reconciliation)

    # Imported here rather than at module scope: live_activation reads this
    # module's report type, and a top-level import would be circular.
    from app.services.live_activation import current_activation

    activation = await current_activation(session)
    gates = [
        LiveGate(
            "runtime_mode",
            "Runtime configured for live",
            settings.application_mode == "LIVE" and settings.live_trading_enabled,
            _runtime_mode_detail(settings),
        ),
        LiveGate(
            "compliance",
            "Compliance approval",
            settings.live_compliance_approved,
            "Operator compliance attestation is required before any future live rollout.",
        ),
        LiveGate(
            "static_ip",
            "Static egress IP",
            settings.live_static_ip_verified,
            "A verified broker allow-listed static egress IP is required.",
        ),
        LiveGate(
            "service_health",
            "Core service health",
            database_healthy and redis_healthy,
            "PostgreSQL and Redis must be healthy at activation time."
            if database_healthy and redis_healthy
            else "PostgreSQL or Redis health check failed.",
        ),
        LiveGate(
            "broker_adapter",
            "Broker execution adapter",
            SUBMISSION_ADAPTER_IMPLEMENTED,
            "No broker submission adapter is implemented; configuration cannot change this boundary."
            if not SUBMISSION_ADAPTER_IMPLEMENTED
            else "A broker submission adapter is present.",
        ),
        LiveGate(
            "live_risk_engine",
            "Live risk revalidation",
            True,
            "A live risk engine exists in app.services.live_risk, independent of the paper engine.",
        ),
        LiveGate(
            "external_reconciliation",
            "External reconciliation",
            reconciliation_passed,
            reconciliation_detail,
        ),
        LiveGate(
            "administrator_activation",
            "Administrator activation",
            activation is not None,
            f"Armed by an administrator until {activation.expires_at.isoformat()}."
            if activation is not None
            else "No administrator has armed live submission, or the activation expired or was revoked.",
        ),
    ]
    # Computed from the gates, so a gate that is added and never wired in cannot
    # leave the report claiming a readiness nobody evaluated.
    overall_ready = all(gate.passed for gate in gates)
    return LiveReadinessReport(
        status="READY" if overall_ready else "HARD_LOCKED",
        overall_ready=overall_ready,
        checked_at=datetime.now(UTC),
        gates=gates,
    )


def _runtime_mode_detail(settings: Settings) -> str:
    """Say which half of the configuration is missing, not merely that it is."""
    if settings.application_mode == "LIVE" and settings.live_trading_enabled:
        return "APPLICATION_MODE is LIVE and LIVE_TRADING_ENABLED is set."
    missing = []
    if settings.application_mode != "LIVE":
        missing.append(f"APPLICATION_MODE is {settings.application_mode}")
    if not settings.live_trading_enabled:
        missing.append("LIVE_TRADING_ENABLED is false")
    return "The runtime is not configured for live trading: " + ", ".join(missing) + "."


def _reconciliation_gate(record: ExecutionReconciliation | None) -> tuple[bool, str]:
    """A live reconciliation that cleared the account, recently."""
    if record is None:
        return False, "No live reconciliation has been recorded against the broker account."
    if not record.safe_to_trade:
        return False, f"The latest live reconciliation blocked trading: {record.detail}"
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created_at
    if age > RECONCILIATION_FRESHNESS:
        minutes = int(age.total_seconds() // 60)
        return False, f"The latest live reconciliation is {minutes} minutes old; re-run it before activation."
    return True, "Broker and local state agreed within the freshness window."


async def persist_live_readiness_check(
    session: AsyncSession, report: LiveReadinessReport, user: User
) -> LiveReadinessCheck:
    check = LiveReadinessCheck(
        status=report.status,
        overall_ready=report.overall_ready,
        gate_snapshot=report.snapshot(),
        checked_by_user_id=user.id,
    )
    session.add(check)
    await session.flush()
    return check
