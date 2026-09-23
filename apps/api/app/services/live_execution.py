"""Decide whether a live order may be sent, and send it.

Phase 4. ``live_orders`` knows how to place an order; this module knows whether
one may be placed. They are separate on purpose: the code that can reach a
broker contains no conditions, so no edit to a condition can make it reach
further, and the conditions live in one file where they can all be read at once.

The gates, in the order they are checked and with the reason each exists:

``emergency_stop``
    Someone pressed stop. That is the one signal with no nuance, so it is
    checked first and no later gate can override it.

``unresolved_submissions``
    An earlier order's outcome is still unknown. Sending another while one may
    already be live is how a position doubles without anyone deciding to double
    it. This blocks the entire live path, not just the instrument.

``activation``
    An administrator armed the system, recently, and has not revoked it.
    Configuration says live trading is possible; this says it is armed right
    now, and it expires on its own so the default state later is off.

``approval_mode``
    DISABLED, TELEGRAM_APPROVAL or AUTOMATIC — who authorises, from settings.

``live_risk``
    The per-order engine from Phase 2: readiness, reconciliation freshness,
    quantity, whether the instrument can be named at this broker at all, the
    day's profit target and loss limit measured against the broker's own P&L,
    and broker margin. It is called here rather than reimplemented.

``operator_approval``
    Under TELEGRAM_APPROVAL, a human said yes and the risk was revalidated at
    the moment they said it, not at the moment they were asked.

A refusal is the default at every step. The function returns a decision object
whose ``authorized`` is computed from the collected gates, so a gate added
without being wired into the result refuses rather than passes.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveActivation, LiveOrderSubmission
from app.services.broker_adapter import BrokerAdapter, BrokerOrderDescription
from app.services.live_orders import (
    LiveOrderRequest,
    SubmissionOutcome,
    apply_outcome,
    new_client_order_id,
    prepare_submission,
    send_prepared_order,
)
from app.services.live_risk import APPROVAL_MODES_PERMITTING_SUBMISSION, authorize_live_order
from app.services.safety import emergency_stop_state

logger = logging.getLogger(__name__)

# Submission states that mean an earlier attempt is still open.
BLOCKING_SUBMISSION_STATES = ("PREPARED", "UNKNOWN", "NEEDS_REVIEW")


@dataclass(frozen=True)
class Gate:
    key: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class LiveExecutionDecision:
    authorized: bool
    reason: str
    checked_at: datetime
    gates: list[Gate] = field(default_factory=list)

    @property
    def failures(self) -> list[Gate]:
        return [gate for gate in self.gates if not gate.passed]

    def snapshot(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
            "gates": [asdict(gate) for gate in self.gates],
        }


async def _emergency_stop_gate(redis: Redis) -> Gate:
    """A stop that cannot be read is treated as engaged.

    The alternative — assuming it is clear when Redis is unreachable — makes the
    kill switch fail open, which is the one behaviour a kill switch must never
    have.
    """
    try:
        state = await emergency_stop_state(redis)
    except Exception as exc:
        return Gate("emergency_stop", False, f"Emergency-stop state could not be read: {exc}")
    if state and str(state.get("active", "")).lower() == "true":
        return Gate("emergency_stop", False, f"Emergency stop is engaged: {state.get('reason', 'no reason recorded')}")
    return Gate("emergency_stop", True, "No emergency stop is engaged.")


async def _unresolved_submission_gate(session: AsyncSession) -> Gate:
    rows = list(
        (
            await session.scalars(
                select(LiveOrderSubmission.client_order_id)
                .where(LiveOrderSubmission.status.in_(BLOCKING_SUBMISSION_STATES))
                .limit(5)
            )
        ).all()
    )
    if rows:
        return Gate(
            "unresolved_submissions",
            False,
            f"{len(rows)} earlier submission(s) have no confirmed outcome: {', '.join(rows)}. "
            "Resolve them against the order book before sending anything else.",
        )
    return Gate("unresolved_submissions", True, "Every previous submission has a confirmed outcome.")


async def _activation_gate(session: AsyncSession) -> Gate:
    now = datetime.now(UTC)
    record = await session.scalar(select(LiveActivation).order_by(LiveActivation.created_at.desc()).limit(1))
    if record is None:
        return Gate("activation", False, "Live trading has never been activated by an administrator.")
    if record.revoked_at is not None:
        return Gate("activation", False, f"Live activation was revoked: {record.revoked_reason or 'no reason given'}")
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return Gate("activation", False, f"Live activation expired at {expires_at.isoformat()}.")
    return Gate("activation", True, f"Armed by an administrator until {expires_at.isoformat()}.")


async def authorize_live_submission(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    redis: Redis,
    *,
    approval_mode: str,
    request: LiveOrderRequest,
    description: BrokerOrderDescription,
    client_order_id: str,
    operator_approved: bool | None = None,
) -> LiveExecutionDecision:
    """Every gate, every time, in one place.

    ``operator_approved`` is None under AUTOMATIC and a decision under
    TELEGRAM_APPROVAL. Passing None while the mode requires approval refuses:
    the absence of an answer is not an answer.
    """
    checked_at = datetime.now(UTC)
    gates: list[Gate] = [
        await _emergency_stop_gate(redis),
        await _unresolved_submission_gate(session),
        await _activation_gate(session),
    ]

    normalized_mode = (approval_mode or "").strip().upper()
    gates.append(
        Gate(
            "approval_mode",
            normalized_mode in APPROVAL_MODES_PERMITTING_SUBMISSION,
            f"Execution approval mode is {normalized_mode or 'unset'}.",
        )
    )

    risk = await authorize_live_order(
        session,
        settings,
        adapter,
        approval_mode=normalized_mode,
        order=request.to_broker_order(client_order_id),
        description=description,
        # The submission path, so a day that has reached its limit is closed
        # here rather than re-decided on the next order. The shadow evaluator
        # calls the same engine and deliberately does not pass this.
        record_halt=True,
    )
    gates.append(
        Gate(
            "live_risk",
            risk.authorized,
            risk.reason if not risk.authorized else "Live risk engine authorised this order.",
        )
    )

    if normalized_mode == "TELEGRAM_APPROVAL":
        gates.append(
            Gate(
                "operator_approval",
                operator_approved is True,
                "An operator approved this order."
                if operator_approved is True
                else "This order has no operator approval; Telegram approval mode requires one per order.",
            )
        )

    failures = [gate for gate in gates if not gate.passed]
    return LiveExecutionDecision(
        authorized=not failures,
        reason="Authorised" if not failures else "; ".join(gate.detail for gate in failures),
        checked_at=checked_at,
        gates=gates,
    )


async def submit_live_order(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    redis: Redis,
    *,
    approval_mode: str,
    request: LiveOrderRequest,
    operator_approved: bool | None = None,
    paper_signal_id: Any = None,
    oms_order_id: Any = None,
    approval_reference: str | None = None,
) -> tuple[LiveExecutionDecision, LiveOrderSubmission | None]:
    """Authorise, write the intent down, commit it, then send. In that order.

    The commit between writing and sending is the whole point, and it is why
    this function owns the transaction rather than accepting one: a caller that
    wrapped the send in an outer transaction would silently undo the guarantee,
    because the intent would not be durable at the moment the request leaves.

    The client order id is minted before authorisation rather than at the point
    of writing, because the margin check inside the risk engine asks the broker
    about *this* order, and an order identified by one id in the question and
    another in the answer is two orders as far as recovery is concerned.
    """
    client_order_id = new_client_order_id()
    # Resolved once, before anything is authorised or written. The broker-facing
    # names are then identical in the margin question, the audit row and the
    # placement — which is what makes the row usable to resolve an UNKNOWN.
    description = await adapter.describe(request.to_broker_order(client_order_id))

    decision = await authorize_live_submission(
        session,
        settings,
        adapter,
        redis,
        approval_mode=approval_mode,
        request=request,
        description=description,
        client_order_id=client_order_id,
        operator_approved=operator_approved,
    )
    if not decision.authorized:
        return decision, None

    record = await prepare_submission(
        session,
        request,
        description,
        broker=adapter.name,
        client_order_id=client_order_id,
        paper_signal_id=paper_signal_id,
        oms_order_id=oms_order_id,
        approval_reference=approval_reference,
    )
    # Durable before the request leaves. Everything downstream depends on this.
    await session.commit()
    await session.refresh(record)

    try:
        outcome = await send_prepared_order(adapter, record, request, description)
    except Exception as exc:
        # An unexpected failure in our own code after the send may still have
        # sent it. Unknown is the only honest classification.
        logger.exception("Live submission raised after the intent was committed")
        outcome = SubmissionOutcome(status="UNKNOWN", detail=f"Submission raised locally: {exc}")

    apply_outcome(record, outcome)
    await session.commit()
    await session.refresh(record)
    return decision, record
