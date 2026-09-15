"""Per-order operator approval over Telegram, for live submission.

The operator asked for this to be the first live mode, and it is the right one:
a human sees every order before it exists. But a human in the loop is only a
safeguard if the loop is built correctly, and there are three ways to build it
wrongly.

**An approval is not permission to submit later.** Between the alert and the
reply the price moves, margin changes, and an earlier order may have filled. So
the answer is revalidated against the full live gate set at the moment it
arrives — not at the moment it was asked for, and not against a snapshot taken
then. An approval that cannot be revalidated is refused, and the operator is
told why rather than left to assume it went through.

**An approval must not be usable twice.** A double tap on Telegram, a retried
webhook delivery, or two devices answering the same message must produce one
order. The approval row is locked and its terminal status checked inside the
same transaction that acts on it, so the second attempt finds the first already
decided and does nothing.

**An approval must expire.** An unanswered alert is not a pending decision an
hour later; it is a stale one. Expiry is short by default because the price the
approval was requested at stops being the price.

This module is deliberately separate from ``assisted_trading``, which is
paper-only and revalidates through the paper risk engine. Sharing them would
mean a change made for paper approvals could alter what reaches a broker.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveOrderApproval, LiveOrderSubmission
from app.services.firstock.orders import FirstockOrderClient
from app.services.live_execution import submit_live_order
from app.services.live_orders import LiveOrderRequest
from app.services.telegram import TelegramError, TelegramNotificationService

logger = logging.getLogger(__name__)

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
BLOCKED = "BLOCKED"
SUBMITTED = "SUBMITTED"

TERMINAL_STATUSES = frozenset({APPROVED, REJECTED, EXPIRED, BLOCKED, SUBMITTED})

# Distinct from the paper flow's "sentinel:" prefix. A live approval reaching the
# paper handler, or the reverse, must be impossible rather than unlikely.
CALLBACK_PREFIX = "live"
APPROVE_ACTION = "approve"
REJECT_ACTION = "reject"


@dataclass(frozen=True)
class ApprovalDecisionResult:
    status: str
    detail: str
    submission: LiveOrderSubmission | None = None


def new_reference_id() -> str:
    """Short enough for Telegram callback data, unique without coordination."""
    return f"la-{uuid.uuid4().hex[:16]}"


def approval_message(approval: LiveOrderApproval) -> str:
    """What the operator reads before deciding.

    States the instrument, the side, the size and the money at stake, because an
    approval button with only a symbol on it trains people to tap yes.
    """
    side = "BUY" if approval.transaction_type == "B" else "SELL"
    notional = Decimal(approval.quantity) * approval.price
    return (
        "<b>LIVE ORDER — approval required</b>\n"
        f"{side} <b>{approval.quantity}</b> × <b>{approval.trading_symbol}</b> ({approval.exchange})\n"
        f"Limit {approval.price} · notional ≈ {notional}\n"
        f"Product {approval.product} · type {approval.price_type}\n"
        f"Expires {approval.expires_at.strftime('%H:%M:%S')} UTC\n"
        "\nApproving sends a real order with real money."
    )


def approval_keyboard(reference_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Approve LIVE order",
                    "callback_data": f"{CALLBACK_PREFIX}:{APPROVE_ACTION}:{reference_id}",
                },
                {"text": "❌ Reject", "callback_data": f"{CALLBACK_PREFIX}:{REJECT_ACTION}:{reference_id}"},
            ],
            [{"text": "🛑 EMERGENCY STOP", "callback_data": "sentinel:emergency_stop"}],
        ]
    }


async def request_live_approval(
    session: AsyncSession,
    settings: Settings,
    *,
    request: LiveOrderRequest,
    instrument_token: str,
    paper_signal_id=None,  # noqa: ANN001
) -> LiveOrderApproval:
    """Record the pending decision, then ask. In that order.

    The row is committed before the message goes out so that a reply cannot
    arrive referencing an approval this system has not yet stored — the same
    write-ahead reasoning that governs submission, for the same reason.
    """
    approval = LiveOrderApproval(
        reference_id=new_reference_id(),
        paper_signal_id=paper_signal_id,
        instrument_token=instrument_token,
        trading_symbol=request.trading_symbol,
        exchange=request.exchange,
        product=request.product,
        price_type=request.price_type,
        transaction_type=request.transaction_type,
        quantity=request.quantity,
        price=request.price,
        status=PENDING,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.live_approval_expiry_seconds),
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    try:
        await TelegramNotificationService(settings).send_message(
            approval_message(approval), approval_keyboard(approval.reference_id), parse_mode="HTML"
        )
    except TelegramError as exc:
        # An approval nobody was asked for must not sit pending until it expires
        # and look like an operator who ignored it.
        approval.status = BLOCKED
        approval.block_reason = f"Telegram alert failed: {exc}"[:500]
        approval.decided_at = datetime.now(UTC)
        await session.commit()
        logger.warning("Live approval request could not be delivered: %s", exc)
    return approval


def _request_from(approval: LiveOrderApproval) -> LiveOrderRequest:
    return LiveOrderRequest(
        exchange=approval.exchange,
        trading_symbol=approval.trading_symbol,
        product=approval.product,
        price_type=approval.price_type,
        transaction_type=approval.transaction_type,
        quantity=approval.quantity,
        price=approval.price,
    )


async def decide_live_approval(
    session: AsyncSession,
    settings: Settings,
    client: FirstockOrderClient,
    redis: Redis,
    *,
    reference_id: str,
    action: str,
    decided_by: str | None,
    approval_mode: str,
) -> ApprovalDecisionResult:
    """Act on an operator's answer, once.

    The row is locked for update and its status checked inside the same
    transaction, so a repeated webhook delivery or a double tap resolves to one
    order rather than two.
    """
    approval = await session.scalar(
        select(LiveOrderApproval).where(LiveOrderApproval.reference_id == reference_id).with_for_update()
    )
    if approval is None:
        return ApprovalDecisionResult(BLOCKED, "No such approval request.")
    if approval.status in TERMINAL_STATUSES:
        return ApprovalDecisionResult(approval.status, f"Already {approval.status.lower()}; nothing was sent.")

    now = datetime.now(UTC)
    expires_at = approval.expires_at if approval.expires_at.tzinfo else approval.expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        approval.status, approval.decided_at = EXPIRED, now
        approval.block_reason = "The approval expired before it was answered."
        await session.commit()
        return ApprovalDecisionResult(EXPIRED, "This approval expired; nothing was sent.")

    approval.decided_by = decided_by
    approval.decided_at = now

    if action == REJECT_ACTION:
        approval.decision, approval.status = "REJECT", REJECTED
        await session.commit()
        return ApprovalDecisionResult(REJECTED, "Rejected. Nothing was sent.")

    if action != APPROVE_ACTION:
        approval.status = BLOCKED
        approval.block_reason = f"Unrecognised decision {action!r}."
        await session.commit()
        return ApprovalDecisionResult(BLOCKED, "Unrecognised decision; nothing was sent.")

    approval.decision = "APPROVE"
    # Mark it used before submitting. If the submission raises, the approval is
    # still spent: a retry must go through a fresh approval rather than reuse
    # one whose outcome nobody knows.
    approval.status = APPROVED
    await session.commit()

    decision, submission = await submit_live_order(
        session,
        settings,
        client,
        redis,
        approval_mode=approval_mode,
        request=_request_from(approval),
        operator_approved=True,
        paper_signal_id=approval.paper_signal_id,
        approval_reference=approval.reference_id,
    )

    approval = await session.scalar(select(LiveOrderApproval).where(LiveOrderApproval.reference_id == reference_id))
    if approval is None:  # pragma: no cover - the row was just written
        return ApprovalDecisionResult(BLOCKED, "Approval record disappeared during submission.")

    approval.revalidation_snapshot = decision.snapshot()
    if not decision.authorized:
        approval.status = BLOCKED
        approval.block_reason = decision.reason[:500]
        await session.commit()
        return ApprovalDecisionResult(BLOCKED, f"Approved, but blocked on revalidation: {decision.reason}")

    approval.status = SUBMITTED
    await session.commit()
    detail = (
        f"Sent. Broker order {', '.join(submission.broker_order_numbers)}"
        if submission and submission.broker_order_numbers
        else f"Sent; outcome {submission.status if submission else 'unknown'}."
    )
    return ApprovalDecisionResult(SUBMITTED, detail, submission)


async def expire_stale_approvals(session: AsyncSession) -> int:
    """Close out approvals nobody answered.

    Without this a pending row stays pending forever and the record stops
    distinguishing "not yet answered" from "never answered".
    """
    now = datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(LiveOrderApproval).where(
                    LiveOrderApproval.status == PENDING, LiveOrderApproval.expires_at <= now
                )
            )
        ).all()
    )
    for approval in rows:
        approval.status = EXPIRED
        approval.decided_at = now
        approval.block_reason = "Expired without an answer."
    if rows:
        await session.flush()
    return len(rows)
