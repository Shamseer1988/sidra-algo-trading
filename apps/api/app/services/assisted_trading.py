"""Paper-only assisted approval decisions; intentionally stops before broker submission."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PaperSignal, TradeApprovalIntent
from app.services.risk_engine import PaperRiskEngine


async def decide_approval(session: AsyncSession, approval: TradeApprovalIntent, decision: str) -> TradeApprovalIntent:
    now = datetime.now(UTC)
    if approval.status in {"APPROVED_PAPER_ONLY", "REJECTED", "EXPIRED"}:
        return approval
    if approval.expires_at and approval.expires_at <= now:
        approval.status, approval.decided_at = "EXPIRED", now
        approval.submission_block_reason = "Approval expired before risk revalidation"
        return approval
    if decision == "REJECT":
        approval.decision, approval.status, approval.decided_at = "REJECT", "REJECTED", now
        return approval
    signal = await session.get(PaperSignal, approval.reference_id)
    if signal is None:
        approval.status, approval.decided_at = "REJECTED", now
        approval.submission_block_reason = "Source paper signal is unavailable"
        return approval
    risk = await PaperRiskEngine().reserve_signal(signal)
    approval.decision, approval.decided_at, approval.risk_revalidated_at = "APPROVE", now, now
    approval.status = "APPROVED_PAPER_ONLY" if risk.allowed else "REJECTED"
    approval.submission_block_reason = (
        "Live broker submission is unavailable in this release" if risk.allowed else risk.reason
    )
    return approval
