"""Live-architecture readiness checks that deliberately cannot submit or enable orders."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ExecutionReconciliation, LiveReadinessCheck, User


@dataclass(frozen=True)
class LiveGate:
    key: str
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class LiveReadinessReport:
    status: str
    overall_ready: bool
    checked_at: datetime
    gates: list[LiveGate]

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "overall_ready": self.overall_ready,
            "checked_at": self.checked_at.isoformat(),
            "gates": [asdict(gate) for gate in self.gates],
            "broker_submission_permitted": False,
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

    latest_reconciliation = await session.scalar(
        select(ExecutionReconciliation).order_by(ExecutionReconciliation.created_at.desc()).limit(1)
    )
    reconciliation_detail = (
        "A paper reconciliation is clean, but no external-broker reconciliation adapter exists."
        if latest_reconciliation and latest_reconciliation.status == "CLEAN"
        else "No clean reconciliation checkpoint is available."
    )
    gates = [
        LiveGate(
            "runtime_lock",
            "Runtime hard lock",
            settings.application_mode != "LIVE" and not settings.live_trading_enabled,
            "PAPER/REPLAY configuration is asserted; live activation remains rejected at startup.",
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
            False,
            "No broker submission adapter is implemented; configuration cannot change this boundary.",
        ),
        LiveGate(
            "live_risk_engine",
            "Live risk revalidation",
            False,
            "Only the paper risk engine exists; a distinct live risk engine is mandatory.",
        ),
        LiveGate(
            "external_reconciliation",
            "External reconciliation",
            False,
            reconciliation_detail,
        ),
        LiveGate(
            "administrator_activation",
            "Administrator activation",
            False,
            "There is intentionally no live activation endpoint in this release.",
        ),
    ]
    return LiveReadinessReport(
        status="HARD_LOCKED",
        overall_ready=False,
        checked_at=datetime.now(UTC),
        gates=gates,
    )


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
