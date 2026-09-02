"""Protected paper-only assisted approval APIs."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, TradeApprovalIntent, User, UserRole
from app.services.assisted_trading import decide_approval

router = APIRouter(prefix="/assisted", tags=["Assisted trading"])


class ApprovalResponse(BaseModel):
    reference_id: str
    decision: str
    source: str
    status: str
    expires_at: datetime | None
    decided_at: datetime | None
    risk_revalidated_at: datetime | None
    submission_block_reason: str | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    decision: str


@router.get("/approvals", response_model=list[ApprovalResponse])
async def approvals(_: CurrentUser, session: DbSession) -> list[ApprovalResponse]:
    rows = list(
        (
            await session.scalars(
                select(TradeApprovalIntent).order_by(TradeApprovalIntent.created_at.desc()).limit(100)
            )
        ).all()
    )
    return [_row(item) for item in rows]


@router.post("/approvals/{reference_id}/decision", response_model=ApprovalResponse)
async def decision(
    reference_id: str,
    payload: ApprovalDecision,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ApprovalResponse:
    normalized = payload.decision.upper()
    if normalized not in {"APPROVE", "REJECT"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Decision must be APPROVE or REJECT"
        )
    approval = await session.scalar(
        select(TradeApprovalIntent).where(TradeApprovalIntent.reference_id == reference_id).with_for_update()
    )
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")
    await decide_approval(session, approval, normalized)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="assisted.approval_decided",
            metadata_json={"reference_id": reference_id, "status": approval.status, "paper_only": True},
        )
    )
    await session.commit()
    await session.refresh(approval)
    return _row(approval)


def _row(item: TradeApprovalIntent) -> ApprovalResponse:
    return ApprovalResponse(
        reference_id=item.reference_id,
        decision=item.decision,
        source=item.source,
        status=item.status,
        expires_at=item.expires_at,
        decided_at=item.decided_at,
        risk_revalidated_at=item.risk_revalidated_at,
        submission_block_reason=item.submission_block_reason,
        created_at=item.created_at,
    )
