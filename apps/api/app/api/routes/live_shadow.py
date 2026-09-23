"""Live shadow evidence, and the live reconciliation that feeds it.

Every endpoint here reads. The one POST runs a live reconciliation, which asks
the broker for its order book and position book and writes down the verdict; it
creates, modifies and cancels nothing. There is deliberately no endpoint that
submits an order, and none that authorises one — the live risk engine is called
only by the shadow evaluator, which records its answer instead of acting on it.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, LiveOrderSubmission, LiveShadowDecision, User, UserRole
from app.services.live_activation import (
    LiveActivationError,
    activate_live_trading,
    current_activation,
    revoke_live_activation,
)
from app.services.live_execution_gateway import BrokerNotSelectedError, live_report_adapter
from app.services.live_order_recovery import resolve_submission, unresolved_submissions
from app.services.live_readiness import inspect_live_readiness
from app.services.live_reconciliation import persist_live_reconciliation, reconcile_live_execution
from app.services.live_shadow import summarize_live_shadow

router = APIRouter(prefix="/live-shadow", tags=["Live shadow"])


class LiveShadowDecisionResponse(BaseModel):
    id: str
    instrument_token: str
    translation_status: str
    trading_symbol: str | None
    exchange: str | None
    product: str | None
    price_type: str | None
    transaction_type: str | None
    quantity: int
    price: float | None
    authorized: bool
    reason: str
    failed_checks: list[str]
    approval_mode: str
    broker_margin_required: float | None
    broker_margin_available: float | None
    created_at: datetime


class RefusalCount(BaseModel):
    check: str
    count: int


class LiveShadowSummaryResponse(BaseModel):
    evaluated: int
    authorized: int
    refused: int
    unresolved_symbols: int
    authorization_rate_percent: float
    top_refusals: list[RefusalCount]
    # Stated in the payload rather than left to the reader to infer.
    broker_submission_permitted: bool = False


class LiveReconciliationResponse(BaseModel):
    id: str
    status: str
    safe_to_trade: bool
    internal_orders: int
    external_orders: int
    unknown_orders: int
    detail: str
    findings: list[dict]
    created_at: datetime


@router.get("/decisions", response_model=list[LiveShadowDecisionResponse])
async def decisions(
    _: CurrentUser,
    session: DbSession,
    limit: int = Query(default=250, ge=1, le=1000),
) -> list[LiveShadowDecisionResponse]:
    rows = list(
        (
            await session.scalars(
                select(LiveShadowDecision).order_by(LiveShadowDecision.created_at.desc()).limit(limit)
            )
        ).all()
    )
    return [_decision(row) for row in rows]


@router.get("/summary", response_model=LiveShadowSummaryResponse)
async def summary(_: CurrentUser, session: DbSession) -> LiveShadowSummaryResponse:
    result = await summarize_live_shadow(session)
    return LiveShadowSummaryResponse(
        evaluated=result.evaluated,
        authorized=result.authorized,
        refused=result.refused,
        unresolved_symbols=result.unresolved_symbols,
        authorization_rate_percent=result.authorization_rate_percent,
        top_refusals=[RefusalCount(**item) for item in result.top_refusals],
    )


@router.post("/reconcile", response_model=LiveReconciliationResponse)
async def reconcile(
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> LiveReconciliationResponse:
    """Compare broker state against ours and record the verdict.

    Reads only. A broker failure is not an error here — it produces a blocked
    reconciliation, because being unable to check is itself a reason not to
    trade. Only an unselected or unreachable broker is rejected, since there is
    nothing to compare against and a recorded verdict would be misleading.
    """
    try:
        adapter = await live_report_adapter(settings, session)
    except BrokerNotSelectedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Broker login failed: {exc}") from exc

    report = await reconcile_live_execution(session, adapter)
    record = await persist_live_reconciliation(session, report)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="live.reconciled",
            metadata_json={
                "status": report.status,
                "safe_to_trade": report.safe_to_trade,
                "submitted_orders": 0,
            },
        )
    )
    await session.commit()
    await session.refresh(record)
    return LiveReconciliationResponse(
        id=str(record.id),
        status=record.status,
        safe_to_trade=record.safe_to_trade,
        internal_orders=record.internal_orders,
        external_orders=record.external_orders,
        unknown_orders=record.unknown_orders,
        detail=record.detail,
        findings=list(record.findings or []),
        created_at=record.created_at,
    )


def _decision(row: LiveShadowDecision) -> LiveShadowDecisionResponse:
    return LiveShadowDecisionResponse(
        id=str(row.id),
        instrument_token=row.instrument_token,
        translation_status=row.translation_status,
        trading_symbol=row.trading_symbol,
        exchange=row.exchange,
        product=row.product,
        price_type=row.price_type,
        transaction_type=row.transaction_type,
        quantity=row.quantity,
        price=float(row.price) if row.price is not None else None,
        authorized=row.authorized,
        reason=row.reason,
        failed_checks=[str(item) for item in (row.failed_checks or [])],
        approval_mode=row.approval_mode,
        broker_margin_required=(float(row.broker_margin_required) if row.broker_margin_required is not None else None),
        broker_margin_available=(
            float(row.broker_margin_available) if row.broker_margin_available is not None else None
        ),
        created_at=row.created_at,
    )


class ActivationRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=255)


class ActivationResponse(BaseModel):
    id: str | None
    armed: bool
    reason: str
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    blocking_gates: list[str]


class UnresolvedSubmissionResponse(BaseModel):
    id: str
    client_order_id: str
    trading_symbol: str
    transaction_type: str
    quantity: int
    status: str
    resolution_attempts: int
    resolution_detail: str | None
    created_at: datetime


@router.get("/activation", response_model=ActivationResponse)
async def activation_status(_: CurrentUser, session: DbSession, settings: AppSettings) -> ActivationResponse:
    record = await current_activation(session)
    report = await inspect_live_readiness(session, settings)
    return ActivationResponse(
        id=str(record.id) if record else None,
        armed=record is not None,
        reason=record.reason if record else "",
        expires_at=record.expires_at if record else None,
        revoked_at=record.revoked_at if record else None,
        revoked_reason=record.revoked_reason if record else None,
        blocking_gates=report.blocking_activation,
    )


@router.post("/activation", response_model=ActivationResponse)
async def arm_live_trading(
    payload: ActivationRequest,
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ActivationResponse:
    """Arm live submission for a bounded window.

    Refuses unless every other readiness gate passes. A reason is required and
    stored: an armed trading system should be able to say who armed it and why.
    """
    report = await inspect_live_readiness(session, settings)
    try:
        record = await activate_live_trading(session, settings, report, user, reason=payload.reason)
    except LiveActivationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="live.activated",
            metadata_json={"reason": payload.reason, "expires_at": record.expires_at.isoformat()},
        )
    )
    await session.commit()
    await session.refresh(record)
    return ActivationResponse(
        id=str(record.id),
        armed=True,
        reason=record.reason,
        expires_at=record.expires_at,
        revoked_at=None,
        revoked_reason=None,
        blocking_gates=[],
    )


@router.delete("/activation", response_model=ActivationResponse)
async def disarm_live_trading(
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ActivationResponse:
    """Disarm immediately. Succeeds even when nothing was armed."""
    record = await revoke_live_activation(session, reason=f"Revoked by {user.email}")
    session.add(AuditLog(user_id=user.id, event_type="live.deactivated", metadata_json={}))
    await session.commit()
    report = await inspect_live_readiness(session, settings)
    return ActivationResponse(
        id=str(record.id) if record else None,
        armed=False,
        reason=record.reason if record else "",
        expires_at=record.expires_at if record else None,
        revoked_at=record.revoked_at if record else None,
        revoked_reason=record.revoked_reason if record else None,
        blocking_gates=report.blocking_activation,
    )


@router.get("/submissions/unresolved", response_model=list[UnresolvedSubmissionResponse])
async def unresolved(_: CurrentUser, session: DbSession) -> list[UnresolvedSubmissionResponse]:
    """Submissions whose outcome is still open. Any row here blocks live trading."""
    rows = await unresolved_submissions(session)
    return [
        UnresolvedSubmissionResponse(
            id=str(row.id),
            client_order_id=row.client_order_id,
            trading_symbol=row.trading_symbol,
            transaction_type=row.transaction_type,
            quantity=row.quantity,
            status=row.status,
            resolution_attempts=row.resolution_attempts,
            resolution_detail=row.resolution_detail,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/submissions/{client_order_id}/resolve", response_model=UnresolvedSubmissionResponse)
async def resolve_unknown_submission(
    client_order_id: str,
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> UnresolvedSubmissionResponse:
    """Look one ambiguous submission up in the broker's order book.

    Uses the read-only client: resolving an uncertain order must not be able to
    place or cancel anything, or one uncertain order becomes two certain ones.
    """
    record = await session.scalar(
        select(LiveOrderSubmission).where(LiveOrderSubmission.client_order_id == client_order_id)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such submission")
    try:
        adapter = await live_report_adapter(settings, session, record.broker or None)
    except BrokerNotSelectedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Broker login failed: {exc}") from exc

    # Resolved against the broker the order was sent to, which is not necessarily
    # the one currently selected. Looking for it in the wrong order book would
    # find nothing and escalate an order that is sitting there plainly visible.
    result = await resolve_submission(adapter, record)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="live.submission_resolution_attempted",
            metadata_json={"client_order_id": client_order_id, "status": result.status},
        )
    )
    await session.commit()
    await session.refresh(record)
    return UnresolvedSubmissionResponse(
        id=str(record.id),
        client_order_id=record.client_order_id,
        trading_symbol=record.trading_symbol,
        transaction_type=record.transaction_type,
        quantity=record.quantity,
        status=record.status,
        resolution_attempts=record.resolution_attempts,
        resolution_detail=record.resolution_detail,
        created_at=record.created_at,
    )
