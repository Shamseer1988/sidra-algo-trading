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
logged with its reason, and a refusal the operator would not otherwise see is
sent to Telegram.

That last part originally covered only the refusals *after* an order was
attempted, which left the whole pre-ask path silent: under TELEGRAM_APPROVAL an
operator who had armed the system saw a paper alert, no approval buttons, and
no explanation, because the gate check that stopped it returned quietly into a
container log. ``ANNOUNCED_REFUSALS`` below names which refusals are sent and,
just as deliberately, which are not -- a deployment configured not to trade live
must not alert on every signal it declines by design.

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
from hashlib import sha256
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting, LiveOrderApproval, LiveOrderSubmission, PaperSignal
from app.services.entry_pricing import DEFAULT_CAP_PERCENT, plan_entry
from app.services.live_approval import BLOCKED, request_live_approval
from app.services.live_execution import submit_live_order
from app.services.live_execution_gateway import BrokerNotSelectedError, live_order_adapter
from app.services.live_orders import LiveOrderRequest
from app.services.live_protection import protect_after_fill
from app.services.live_readiness import inspect_live_readiness
from app.services.live_shadow import product_for, transaction_type_for

logger = logging.getLogger(__name__)

TELEGRAM_APPROVAL = "TELEGRAM_APPROVAL"
AUTOMATIC = "AUTOMATIC"
DISABLED = "DISABLED"

# Which refusals reach the operator, and why the rest do not.
#
# The module docstring says silence is not an acceptable refusal, and until now
# that held only for refusals *after* an order was attempted. Everything before
# that point -- a lapsed gate, an unnameable instrument, a broker that stopped
# answering -- returned quietly and was visible only in container logs. Under
# TELEGRAM_APPROVAL that is the whole of the pre-ask path, so an operator armed
# and waiting saw a paper alert, no approval buttons, and nothing to explain it.
#
# The test for inclusion is whether the refusal contradicts what the operator
# believes. A deployment configured not to trade live refuses every signal by
# design; saying so each time would bury the refusals that matter. A gate that
# failed after the operator armed the system is the opposite: it is the message
# they most need and the one they were least likely to get.
ANNOUNCED_REFUSALS = frozenset(
    {
        "gates",  # armed, then something lapsed
        "instrument",  # the symbol cannot be named at this broker
        "signal",  # the signal itself will not convert to an order
        "broker_unavailable",  # a broker was chosen and did not answer
        "approval_undelivered",  # the ask itself never reached Telegram
        "error",  # a bug on this path
    }
)

# Deliberately NOT announced. Spelled out as a set rather than left implicit,
# because "not in the announce set" is how the original silence happened: a
# refusal nobody classified is a refusal nobody hears. A test asserts that every
# step this module can return appears in one set or the other, so a refusal
# added later cannot join the silent ones by omission.
SILENT_REFUSALS = frozenset(
    {
        "runtime",  # this deployment is paper; every signal would alert
        "approval_mode",  # DISABLED, or an unrecognised mode: a configuration choice
        "broker",  # no broker selected: likewise deliberate
        "duplicate",  # a re-delivered candle -- the deduplication working
        "refused",  # _announce_automatic already reported it, with more detail
        "unprotected",  # likewise, and far more loudly than this would
    }
)

# One message per distinct reason, not per signal. A failing gate refuses every
# signal for the rest of the session, and five hours of identical alerts is how
# an operator learns to swipe these away without reading them -- which would
# reproduce the silence this exists to end, by a different route.
REFUSAL_REPEAT_SECONDS = 1800

_REFUSAL_HEADINGS = {
    "gates": "⛔ <b>LIVE ENTRY BLOCKED</b>",
    "instrument": "\U0001f6ab <b>LIVE ENTRY SKIPPED</b>",
    "signal": "\U0001f6ab <b>LIVE ENTRY SKIPPED</b>",
    "broker_unavailable": "\U0001f6ab <b>LIVE ENTRY SKIPPED</b>",
    "approval_undelivered": "\U0001f6ab <b>APPROVAL NOT DELIVERED</b>",
    "error": "\U0001f6a8 <b>LIVE ENTRY FAILED</b>",
}

_REFUSAL_HINTS = {
    "gates": "Clear this on the Risk screen or every further signal today is skipped too.",
    "broker_unavailable": "Check the broker connection; further signals will be skipped until it answers.",
    "error": "This is a fault on the live path, not a trading decision. Check the API logs.",
}


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
    """The signal, in the canonical order vocabulary, priced and sized to the cap.

    The entry price is carried even for a MARKET order. The broker ignores it,
    but the margin check inside the risk engine asks about a priced order, and a
    margin question carrying zero returns a number that means nothing.

    Under LIMIT the price is not the signal's entry but the worst price the
    trade is still worth taking at, and the quantity is sized from *that* price.
    Both halves are needed: a limit alone bounds the price and not the loss,
    because the loss is price times quantity. Together the budget becomes a
    ceiling rather than an intention.

    Under MARKET nothing is capped, because there is no price on a market order
    to cap. That is the setting that put ₹209 behind a ₹100 budget on 5 October,
    and the catalogue entry says so.
    """
    side = transaction_type_for(signal.side)
    if str(getattr(controls, "live_entry_order_type", "")).upper() != "LIMIT":
        return LiveOrderRequest(
            instrument_token=signal.instrument_token,
            side=side,
            quantity=int(signal.quantity),
            order_type=controls.live_entry_order_type,
            product=product_for(bool(controls.intraday_leverage_enabled)),
            price=Decimal(str(signal.entry_price)),
        )

    plan = plan_entry(
        side=side,
        entry_price=Decimal(str(signal.entry_price)),
        stop_price=Decimal(str(signal.stop_price)),
        quantity=int(signal.quantity),
        risk_budget=Decimal(str(signal.risk_amount)),
        cap_percent=Decimal(str(getattr(controls, "entry_slippage_cap_percent", DEFAULT_CAP_PERCENT))),
    )
    if plan.refusal:
        # Raised rather than returned: the caller already turns a ValueError here
        # into a refusal the operator reads, and a plan nobody can act on must
        # not become an order for the quantity the signal happened to carry.
        raise ValueError(plan.refusal)

    return LiveOrderRequest(
        instrument_token=signal.instrument_token,
        side=side,
        quantity=plan.quantity,
        order_type="LIMIT",
        product=product_for(bool(controls.intraday_leverage_enabled)),
        price=plan.limit_price,
    )


async def _label(session: AsyncSession | None, signal: PaperSignal) -> str:
    """What to call this instrument in a message a person reads.

    The paper alert for a trade says "TATASTEEL" while these said
    "NSE_EQ|INE081A01020" for the very same trade, so the messages about real
    money were the harder ones to read. Resolution is best-effort: a label is
    never worth losing an alert over, and the token is a worse name, not a
    worse alert.
    """
    token = signal.instrument_token
    if session is None:
        return token
    try:
        from app.services.trading_symbols import display_symbol

        name = await display_symbol(session, token, token)
    except Exception:  # noqa: BLE001 - the alert matters, the label does not
        return token
    return f"{name} ({token})" if name != token else token


async def _announce_automatic(
    settings: Settings, signal: PaperSignal, decision, submission, protection=None, session: AsyncSession | None = None
) -> None:
    """Tell the operator what an unattended order did. Never raises.

    Under TELEGRAM_APPROVAL the operator is asked, so they know an order exists
    and the reply tells them how it ended. Under AUTOMATIC nobody is asked --
    and the first live order placed this way was rejected by the broker without
    a single message being sent. The operator found out by reading container
    logs. Automatic means nobody is asked, not nobody is told.
    """
    try:
        from app.services.telegram import TelegramNotificationService
        from app.services.telegram_config import configured_settings

        effective = await configured_settings(settings)
        if not effective.telegram_is_configured:
            return

        label = await _label(session, signal)
        numbers = ", ".join(getattr(submission, "broker_order_numbers", None) or []) or "none"
        status = getattr(submission, "status", "UNKNOWN")
        if not decision.authorized:
            text = (
                "\U0001f6ab <b>LIVE ORDER NOT SENT</b>\n\n"
                f"\U0001f4ca {label}  {signal.side}  {signal.quantity}\n"
                f"\U0001f4dd {decision.reason}"
            )
        elif status in {"REJECTED", "FAILED", "UNKNOWN"}:
            reason = getattr(submission, "failure_message", None) or status
            text = (
                "\u26d4 <b>LIVE ORDER REJECTED</b>\n\n"
                f"\U0001f4ca {label}  {signal.side}  {signal.quantity}\n"
                f"\U0001f4dd {reason}\n\n"
                "<i>Placed automatically; the broker refused it. No position was opened.</i>"
            )
        elif protection is not None and not protection.protected:
            # The loudest message this system sends. An open position with
            # nothing behind it is the state that prompted this whole module.
            text = (
                "\U0001f6a8 <b>POSITION NOT PROTECTED</b>\n\n"
                f"\U0001f4ca {label}  {signal.side}  {protection.quantity or signal.quantity}\n"
                f"\U0001f9fe Broker order: {numbers}\n"
                f"\U0001f4dd {protection.detail}\n\n"
                "<i>Check this position in the broker app now.</i>"
            )
        else:
            # The only branch here an operator may switch off. Everything above
            # reports something going wrong -- refused, rejected, unprotected --
            # and muting a failure does not reduce noise, it removes the way the
            # operator finds out. Checked inside this branch rather than at the
            # top so that cannot be changed by accident later.
            from app.services.notification_settings import wants

            if session is not None and not await wants(session, "order_sent_confirmations"):
                logger.info("live_entry_bridge.sent_confirmation_muted signal_id=%s", signal.id)
                return
            stop_line = f"\U0001f6d1 {protection.detail}\n" if protection is not None else ""
            text = (
                "\u2705 <b>LIVE ORDER SENT</b>\n\n"
                f"\U0001f4ca {label}  {signal.side}  {signal.quantity}\n"
                f"\U0001f9fe Broker order: {numbers}\n"
                f"\U0001f4dd Status: {status}\n"
                f"{stop_line}\n"
                "<i>Placed automatically. Disarm to stop further orders.</i>"
            )
        await TelegramNotificationService(effective).send_message(text, parse_mode="HTML")
    except Exception:  # noqa: BLE001 - an alert must not undo a placed order
        logger.exception("live_entry_bridge.announce_failed signal_id=%s", signal.id)


async def _should_announce(redis: Redis, outcome: BridgeOutcome) -> bool:
    """True at most once per ``REFUSAL_REPEAT_SECONDS`` for a given reason.

    Keyed on the reason rather than the signal, because the same failing gate
    refuses every signal of the session and the operator needs to be told once,
    not eleven times. A different reason -- the gate clears and the broker then
    stops answering -- is a different key and is announced on its own.

    **A Redis that cannot answer sends the message.** A duplicate alert is a
    nuisance; a suppressed one is the failure this whole function exists to end,
    so the unavailable case fails toward telling the operator.
    """
    digest = sha256(f"{outcome.step}:{outcome.detail}".encode()).hexdigest()[:16]
    try:
        return bool(await redis.set(f"live:bridge:refusal:{digest}", "1", ex=REFUSAL_REPEAT_SECONDS, nx=True))
    except Exception:  # noqa: BLE001 - never let the throttle silence the alert
        logger.warning("live_entry_bridge.refusal_throttle_unavailable step=%s", outcome.step)
        return True


async def _telegram_settings(settings: Settings) -> Settings:
    """The Telegram configuration, preferring the stored one but never requiring it.

    ``configured_settings`` reads an encrypted row from the database. On the
    path that reports a failure that may itself *be* a database failure, letting
    that read raise would lose the alert to the very fault it describes. The
    environment's own token is the fallback, which is what ``request_live_approval``
    has always used.
    """
    try:
        from app.services.telegram_config import configured_settings

        return await configured_settings(settings)
    except Exception:  # noqa: BLE001 - a stored override is a preference, not a requirement
        logger.warning("live_entry_bridge.telegram_config_unreadable; falling back to environment settings")
        return settings


async def _announce_refusal(
    settings: Settings,
    redis: Redis,
    signal: PaperSignal,
    outcome: BridgeOutcome,
) -> None:
    """Tell the operator about a refusal they would otherwise only find in logs.

    Never raises, and never changes the outcome: by the time this runs the
    decision not to trade has already been made and recorded. A failed alert is
    a reporting problem, exactly as it is on the automatic path.

    The instrument token is printed rather than a resolved name, and this is
    the one message where that is deliberate. ``_announce_automatic`` resolves
    a readable label, but it already holds a session for an order that reached
    the broker. Here the thing being reported may *be* a database failure, and
    a lookup for a nicer label would risk losing the message to the very fault
    it is describing. The token always reads.
    """
    if outcome.step not in ANNOUNCED_REFUSALS:
        return
    try:
        from app.services.telegram import TelegramNotificationService

        # Throttled before anything is read, so a gate that is down for the rest
        # of the session costs one database round trip rather than one per
        # signal.
        if not await _should_announce(redis, outcome):
            logger.info("live_entry_bridge.refusal_alert_suppressed signal_id=%s step=%s", signal.id, outcome.step)
            return

        effective = await _telegram_settings(settings)
        if not effective.telegram_is_configured:
            return

        heading = _REFUSAL_HEADINGS.get(outcome.step, "\U0001f6ab <b>LIVE ENTRY SKIPPED</b>")
        hint = _REFUSAL_HINTS.get(outcome.step, "")
        text = (
            f"{heading}\n\n"
            f"\U0001f4ca {signal.instrument_token}  {signal.side}  {signal.quantity}\n"
            f"\U0001f4dd {outcome.detail}\n\n"
            "<i>The paper signal was recorded. No live order was placed"
            f"{' and none was offered for approval' if outcome.step != 'approval_undelivered' else ''}.</i>"
            + (f"\n<i>{hint}</i>" if hint else "")
        )
        await TelegramNotificationService(effective).send_message(text, parse_mode="HTML")
    except Exception:  # noqa: BLE001 - an alert must not become a second failure
        logger.exception("live_entry_bridge.refusal_announce_failed signal_id=%s step=%s", signal.id, outcome.step)


async def offer_live_entry(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    signal: PaperSignal,
) -> BridgeOutcome:
    """Ask for, or place, a live entry for this signal. Never raises."""
    try:
        outcome = await _offer(session, settings, redis, signal)
    except Exception as exc:  # noqa: BLE001 - paper execution must not be lost to this
        logger.exception("live_entry_bridge.unexpected_error signal_id=%s", signal.id)
        outcome = BridgeOutcome(False, "error", f"{type(exc).__name__}: {exc}")
    # Announced here rather than beside each refusal so that a refusal added
    # later is covered by default, and so the unexpected-error path above -- the
    # one nobody writes an alert for -- is covered at all. Only refusals: an
    # outcome that acted has either asked the operator or already reported
    # itself through _announce_automatic.
    if not outcome.acted:
        await _announce_refusal(settings, redis, signal, outcome)
    return outcome


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
        # A different step from the "no broker selected" refusal above, although
        # the exception type is the same. That one is a configuration choice and
        # is silent; this one happens after a broker *was* chosen and could not
        # be reached, which the operator needs to hear about.
        return BridgeOutcome(False, "broker_unavailable", str(exc))

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
        # request_live_approval marks the row BLOCKED when the Telegram send
        # failed, and returned it either way. Reporting that as "asked" would be
        # the same lie this module was written to stop: nobody was asked, the
        # approval will never be answered, and the operator has been told
        # nothing. Whether the follow-up alert gets through is doubtful when
        # Telegram just refused the first one -- but a plain message without a
        # keyboard is a different request, and it costs one attempt to find out.
        if approval.status == BLOCKED:
            return BridgeOutcome(
                False,
                "approval_undelivered",
                approval.block_reason or "The approval request could not be delivered; nobody was asked.",
            )
        return BridgeOutcome(True, "approval_requested", f"Approval {approval.reference_id} requested.")

    # Explicit rather than "everything that is not TELEGRAM_APPROVAL". The
    # fall-through worked while AUTOMATIC was the only other value, but it
    # failed open: a mode added later -- a review queue, a scheduled window --
    # would have placed orders unasked on the day it was introduced. In this
    # module an unrecognised instruction is a refusal.
    if mode != AUTOMATIC:
        return BridgeOutcome(False, "approval_mode", f"Unrecognised approval mode {mode!r}; nothing was sent.")

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
    # Before the alert, because a position must not exist unprotected for any
    # longer than it has to -- not even for the length of a Telegram round trip.
    protection = None
    if decision.authorized and submission is not None:
        protection = await protect_after_fill(session, settings, adapter, submission)

    # Sent after the order, and never allowed to undo it: the order is already
    # at the broker by this point, so a failed alert is a reporting problem, not
    # a trading one.
    await _announce_automatic(settings, signal, decision, submission, protection, session)
    if not decision.authorized:
        return BridgeOutcome(False, "refused", decision.reason)
    if protection is not None and not protection.protected:
        return BridgeOutcome(True, "unprotected", f"Order sent but not protected: {protection.detail}")
    return BridgeOutcome(True, "submitted", f"Live order submitted: {getattr(submission, 'status', 'unknown')}")
