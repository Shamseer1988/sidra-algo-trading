"""Live shadow evidence, and the live reconciliation that feeds it.

Every endpoint here reads. The one POST runs a live reconciliation, which asks
the broker for its order book and position book and writes down the verdict; it
creates, modifies and cancels nothing. There is deliberately no endpoint that
submits an order, and none that authorises one — the live risk engine is called
only by the shadow evaluator, which records its answer instead of acting on it.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, LiveShadowDecision, User, UserRole
from app.services.firstock.client import FirstockClient, FirstockError
from app.services.firstock.orders import FirstockReportClient
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
    trade. Only an unconfigured broker is rejected, since there is nothing to
    compare against and a recorded verdict would be misleading.
    """
    if not settings.firstock_is_configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Firstock credentials are not configured; there is no broker state to reconcile.",
        )
    try:
        broker_session = await FirstockClient(settings).login()
    except FirstockError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Firstock login failed: {exc}") from exc

    report = await reconcile_live_execution(session, FirstockReportClient(settings, broker_session))
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
