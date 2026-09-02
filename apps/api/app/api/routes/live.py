"""Readiness control plane for a future live release; activation is intentionally absent."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, LiveReadinessCheck, User, UserRole
from app.services.live_readiness import (
    LiveReadinessReport,
    inspect_live_readiness,
    persist_live_readiness_check,
)

router = APIRouter(prefix="/live", tags=["Live architecture"])


class LiveGateResponse(BaseModel):
    key: str
    label: str
    passed: bool
    detail: str


class LiveReadinessResponse(BaseModel):
    status: str
    overall_ready: bool
    live_execution_available: bool
    checked_at: datetime
    gates: list[LiveGateResponse]


class LiveReadinessHistoryResponse(BaseModel):
    id: str
    status: str
    overall_ready: bool
    checked_at: datetime
    checked_by_user_id: str | None


def _response(report: LiveReadinessReport) -> LiveReadinessResponse:
    return LiveReadinessResponse(
        status=report.status,
        overall_ready=report.overall_ready,
        live_execution_available=False,
        checked_at=report.checked_at,
        gates=[LiveGateResponse(**gate.__dict__) for gate in report.gates],
    )


@router.get("/readiness", response_model=LiveReadinessResponse)
async def readiness(_: CurrentUser, session: DbSession, settings: AppSettings) -> LiveReadinessResponse:
    return _response(await inspect_live_readiness(session, settings))


@router.post("/readiness/verify", response_model=LiveReadinessResponse)
async def verify_readiness(
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> LiveReadinessResponse:
    report = await inspect_live_readiness(session, settings)
    check = await persist_live_readiness_check(session, report, user)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="live.readiness_verified",
            metadata_json={"check_id": str(check.id), "status": report.status, "overall_ready": False},
        )
    )
    await session.commit()
    return _response(report)


@router.get("/readiness/history", response_model=list[LiveReadinessHistoryResponse])
async def readiness_history(_: CurrentUser, session: DbSession) -> list[LiveReadinessHistoryResponse]:
    checks = list(
        (
            await session.scalars(select(LiveReadinessCheck).order_by(LiveReadinessCheck.created_at.desc()).limit(20))
        ).all()
    )
    return [
        LiveReadinessHistoryResponse(
            id=str(check.id),
            status=check.status,
            overall_ready=check.overall_ready,
            checked_at=check.created_at,
            checked_by_user_id=str(check.checked_by_user_id) if check.checked_by_user_id else None,
        )
        for check in checks
    ]
