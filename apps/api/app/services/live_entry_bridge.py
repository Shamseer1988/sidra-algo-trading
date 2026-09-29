"""Carry a qualified paper signal to the live order path, or refuse and say why.

This is the link that was missing. Every part on either side of it existed and
was tested -- the scanner produced signals, the gates enforced arming, the
approval flow revalidated and submitted -- and nothing joined them, so a fully
armed deployment could not place an order. The gap was invisible precisely
because both halves looked healthy.

Three rules govern what happens here, and they are why this is its own module
rather than a few lines inside the scanner.

**Paper execution must not be able to lose a trade to this code.** The scanner's
job is the operator's record, and it runs first and completes regardless. So
every failure here is caught, logged and recorded; nothing propagates back into
the candle loop. That is the same posture ``live_shadow_runner`` takes, for the
same reason.

**Silence is not an acceptable refusal.** Shadow mode may fail quietly because
its output is evidence. This path's output is money, and an operator who
approved a signal and saw nothing happen cannot tell a refusal from a bug --
which is exactly the state this module was written to end. Every refusal is
logged with its reason, and a refusal after the point where an operator was
asked is reported to them.

**The gates are re-read here, not trusted from earlier.** ``overall_ready``
covers the activation, the reconciliation and the runtime together, and it is
read at the moment the signal fires rather than carried from the morning. An
activation that lapsed at 16:45 must stop the 16:46 signal.

What this module does *not* do: it does not decide whether an order is sound.
``submit_live_order`` re-authorises against the full live gate set at the moment
of submission, and under TELEGRAM_APPROVAL the operator's answer is revalidated
when it arrives rather than when it was requested. This module only decides
whether to *ask*.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting, LiveOrderApproval, LiveOrderSubmission, PaperSignal
from app.services.live_approval import request_live_approval
from app.services.live_execution import submit_live_order
from app.services.live_execution_gateway import BrokerNotSelectedError, live_order_adapter
from app.services.live_orders import LiveOrderRequest
from app.services.live_readiness import inspect_live_readiness
from app.services.live_shadow import product_for, transaction_type_for

logger = logging.getLogger(__name__)

TELEGRAM_APPROVAL = "TELEGRAM_APPROVAL"
AUTOMATIC = "AUTOMATIC"
DISABLED = "DISABLED"


@dataclass(frozen=True)
class BridgeOutcome:
    """What happened, in terms the tests and the log line both use."""

    acted: bool
    step: str
    detail: str


async def _controls(session: AsyncSession) -> Any:
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

    row = await session.get(ApplicationSetting, TRADING_KEY)
    return TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS)


async def _already_handled(session: AsyncSession, signal_id: Any) -> bool:
    """One live order per signal, ever.

    Checked across both tables because the two approval modes write different
    rows: a re-delivered candle or a restarted worker must not produce a second
    order for a signal that already has one.
    """
    approval = await session.scalar(
        select(LiveOrderApproval.id).where(LiveOrderApproval.paper_signal_id == signal_id).limit(1)
    )
    if approval is not None:
        return True
    submission = await session.scalar(
        select(LiveOrderSubmission.id).where(LiveOrderSubmission.paper_signal_id == signal_id).limit(1)
    )
    return submission is not None


def _order_request(signal: PaperSignal, controls: Any) -> LiveOrderRequest:
    """The signal, in the canonical order vocabulary.

    The entry price is carried even for a MARKET order. The broker ignores it,
    but the margin check inside the risk engine asks about a priced order, and a
    margin question carrying zero returns a number that means nothing.
    """
    return LiveOrderRequest(
        instrument_token=signal.instrument_token,
        side=transaction_type_for(signal.side),
        quantity=int(signal.quantity),
        order_type=controls.live_entry_order_type,
        product=product_for(bool(controls.intraday_leverage_enabled)),
        price=Decimal(str(signal.entry_price)),
    )


async def offer_live_entry(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    signal: PaperSignal,
) -> BridgeOutcome:
    """Ask for, or place, a live entry for this signal. Never raises."""
    try:
        return await _offer(session, settings, redis, signal)
    except Exception as exc:  # noqa: BLE001 - paper execution must not be lost to this
        logger.exception("live_entry_bridge.unexpected_error signal_id=%s", signal.id)
        return BridgeOutcome(False, "error", f"{type(exc).__name__}: {exc}")


async def _offer(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    signal: PaperSignal,
) -> BridgeOutcome:
    # Cheapest refusals first, and the pair together: a deployment that is not
    # both LIVE and enabled has nothing to offer and must not reach a broker to
    # find that out.
    if settings.application_mode != "LIVE" or not settings.live_trading_enabled:
        return BridgeOutcome(False, "runtime", f"Runtime is {settings.application_mode}; no live entry offered.")

    controls = await _controls(session)
    mode = (controls.execution_approval_mode or DISABLED).strip().upper()
    if mode == DISABLED:
        return BridgeOutcome(False, "approval_mode", "Approval mode is DISABLED; no live entry offered.")
    if (controls.live_broker or "NONE").strip().upper() == "NONE":
        return BridgeOutcome(False, "broker", "No live broker is selected.")

    if await _already_handled(session, signal.id):
        return BridgeOutcome(False, "duplicate", "A live order already exists for this signal.")

    # Read now, not carried from the morning: an activation that lapsed between
    # the open and this candle must stop this signal.
    report = await inspect_live_readiness(session, settings)
    if not report.overall_ready:
        blocking = ", ".join(gate.key for gate in report.gates if not gate.passed)
        return BridgeOutcome(False, "gates", f"Live readiness gates not satisfied: {blocking}")

    try:
        request = _order_request(signal, controls)
    except ValueError as exc:
        return BridgeOutcome(False, "signal", str(exc))

    try:
        adapter = await live_order_adapter(settings, session)
    except BrokerNotSelectedError as exc:
        return BridgeOutcome(False, "broker", str(exc))

    # Resolved before anything is written or asked. An instrument this broker
    # cannot name is a refusal the operator should read, not an exception thrown
    # from inside a send.
    description = await adapter.describe(request.to_broker_order("preview"))
    if not description.resolved:
        return BridgeOutcome(
            False,
            "instrument",
            f"{signal.instrument_token} cannot be named at this broker: {description.detail}",
        )

    if mode == TELEGRAM_APPROVAL:
        approval = await request_live_approval(
            session,
            settings,
            request=request,
            description=description,
            broker=adapter.name,
            paper_signal_id=signal.id,
        )
        logger.info(
            "live_entry_bridge.approval_requested signal_id=%s reference=%s status=%s",
            signal.id,
            approval.reference_id,
            approval.status,
        )
        return BridgeOutcome(True, "approval_requested", f"Approval {approval.reference_id} requested.")

    decision, submission = await submit_live_order(
        session,
        settings,
        adapter,
        redis,
        approval_mode=mode,
        request=request,
        paper_signal_id=signal.id,
    )
    logger.info(
        "live_entry_bridge.submitted signal_id=%s authorized=%s status=%s",
        signal.id,
        decision.authorized,
        getattr(submission, "status", None),
    )
    if not decision.authorized:
        return BridgeOutcome(False, "refused", decision.reason)
    return BridgeOutcome(True, "submitted", f"Live order submitted: {getattr(submission, 'status', 'unknown')}")
