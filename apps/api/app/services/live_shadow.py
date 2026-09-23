"""Run the live decision path against a paper signal, and submit nothing.

Phase 3 of the live-execution layer. For every signal the paper system acts on,
this asks the whole live question — can the symbol be named at the broker, is the
account reconciled, does the broker say there is margin, would the live risk
engine authorise it — and writes down the answer.

The reason to do this before submitting anything is that the paper system and
the live system are not the same system, and the places they diverge are not
discoverable by reading the code. A symbol the scanner trades happily every day
may have no verified mapping. Margin at the broker may be a third of the
notional the paper account assumes. Reconciliation may be blocked at 09:20 every
morning by yesterday's position. Each of those turns into a refused live order,
and every one of them is cheaper to find in a table than in a trading session.

Two properties this module must keep:

**It cannot submit.** It holds a read-only adapter, built over a client with no
placement method at all, and it calls the live risk engine, which returns a
verdict rather than performing anything.

**It cannot break paper trading.** Paper execution is the system that is
currently working and producing the results the operator depends on. Shadow
evaluation is an observer of it, so ``shadow_paper_signal`` swallows every
exception: a broker outage, a bad response shape or a bug in this file must cost
an evidence row, never a paper trade.

One modelling decision is recorded here rather than buried. The paper system
places its entry as MARKET, but the shadow evaluates a LIMIT order at the
signal's entry price, for two reasons: a market order on a thin intraday name is
how a live system pays for its slippage, and a margin quote needs a real price to
return a meaningful number. This is a proposal about how live entries should be
placed, and it should be confirmed before Phase 4 makes it real.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveShadowDecision, PaperSignal
from app.services.broker_adapter import (
    BUY,
    DELIVERY,
    INTRADAY,
    LIMIT,
    SELL,
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderDescription,
)
from app.services.live_risk import authorize_live_order
from app.services.live_symbols import SymbolTranslation, translate_for_order

logger = logging.getLogger(__name__)

# The paper system's leverage switch is what decides whether a live order would
# be intraday or delivery, so the shadow reads the same control rather than
# assuming. The broker's own code for each is the adapter's business.
PRODUCT_INTRADAY = INTRADAY
PRODUCT_DELIVERY = DELIVERY

# See the module docstring: the shadow prices entries as limits.
SHADOW_PRICE_TYPE = LIMIT

REASON_MAX_LENGTH = 500


def product_for(intraday_leverage_enabled: bool) -> str:
    return PRODUCT_INTRADAY if intraday_leverage_enabled else PRODUCT_DELIVERY


def transaction_type_for(signal_side: str) -> str:
    """LONG buys, SHORT sells. Anything else is not a side we can trade."""
    side = (signal_side or "").strip().upper()
    if side == "LONG":
        return BUY
    if side == "SHORT":
        return SELL
    raise ValueError(f"Unsupported signal side {signal_side!r}")


async def build_shadow_payload(
    signal: PaperSignal,
    *,
    intraday_leverage_enabled: bool,
) -> BrokerOrder | None:
    """State the signal as a live order, or refuse a side we cannot trade.

    Naming the instrument at the broker is no longer done here: it is the
    adapter's job, and doing it twice is how the shadow would come to evaluate a
    different order than the one live execution would place.
    """
    try:
        side = transaction_type_for(signal.side)
    except ValueError:
        return None

    return BrokerOrder(
        instrument_token=signal.instrument_token,
        side=side,
        quantity=int(signal.quantity),
        order_type=SHADOW_PRICE_TYPE,
        product=product_for(intraday_leverage_enabled),
        price=signal.entry_price,
        # The shadow submits nothing, so the identifier only has to be a
        # plausible one for the margin question to carry.
        client_order_id="shadow",
    )


def _margin_numbers(snapshot: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Pull the broker's own margin figures out of the decision snapshot."""
    for check in snapshot.get("checks", []):
        if check.get("key") != "broker_margin":
            continue
        data = check.get("data") or {}
        return _optional_decimal(data.get("required")), _optional_decimal(data.get("available"))
    return None, None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def evaluate_live_shadow(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    *,
    signal: PaperSignal,
    oms_order_id: UUID | None,
    approval_mode: str,
    intraday_leverage_enabled: bool,
) -> LiveShadowDecision:
    """Decide what the live path would have done, and record it.

    Returns the persisted row. An unresolvable symbol short-circuits before any
    broker call: there is nothing to ask the broker about an instrument we cannot
    name, and the refusal is the finding.
    """
    # Kept for the evidence row, which records the symbol map's verdict whether
    # or not the chosen broker needed it. A broker that takes the token
    # unchanged still benefits from knowing which names are unmapped, because
    # the operator may switch to one that does not.
    translation = await translate_for_order(session, signal.instrument_token)

    order = await build_shadow_payload(signal, intraday_leverage_enabled=intraday_leverage_enabled)
    if order is None:
        reason = f"Unsupported signal side {signal.side!r}"
        return await _persist(
            session,
            signal=signal,
            oms_order_id=oms_order_id,
            approval_mode=approval_mode,
            translation=translation,
            description=None,
            authorized=False,
            reason=reason,
            failed_checks=["signal_side"],
            snapshot={"signal_side": {"resolved": False, "reason": reason}},
        )

    description = await adapter.describe(order)
    if not description.resolved:
        return await _persist(
            session,
            signal=signal,
            oms_order_id=oms_order_id,
            approval_mode=approval_mode,
            translation=translation,
            description=None,
            authorized=False,
            reason=description.detail,
            failed_checks=["symbol_translation"],
            snapshot={"symbol_translation": {"resolved": False, "reason": description.detail}},
        )

    decision = await authorize_live_order(
        session,
        settings,
        adapter,
        approval_mode=approval_mode,
        order=order,
        description=description,
    )
    snapshot = decision.snapshot()
    return await _persist(
        session,
        signal=signal,
        oms_order_id=oms_order_id,
        approval_mode=approval_mode,
        translation=translation,
        description=description,
        authorized=decision.authorized,
        reason=decision.reason,
        failed_checks=[check.key for check in decision.failures],
        snapshot=snapshot,
    )


async def _persist(
    session: AsyncSession,
    *,
    signal: PaperSignal,
    oms_order_id: UUID | None,
    approval_mode: str,
    translation: SymbolTranslation,
    description: BrokerOrderDescription | None,
    authorized: bool,
    reason: str,
    failed_checks: list[str],
    snapshot: dict[str, Any],
) -> LiveShadowDecision:
    required, available = _margin_numbers(snapshot)
    record = LiveShadowDecision(
        paper_signal_id=signal.id,
        oms_order_id=oms_order_id,
        instrument_token=signal.instrument_token,
        translation_status=translation.status,
        trading_symbol=description.symbol if description else None,
        exchange=description.exchange if description else None,
        product=description.product if description else None,
        price_type=description.order_type if description else None,
        transaction_type=description.side if description else None,
        quantity=signal.quantity,
        price=signal.entry_price,
        authorized=authorized,
        reason=reason[:REASON_MAX_LENGTH],
        failed_checks=failed_checks,
        decision_snapshot=snapshot,
        approval_mode=approval_mode,
        broker_margin_required=required,
        broker_margin_available=available,
    )
    session.add(record)
    await session.flush()
    return record


async def shadow_paper_signal(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    *,
    signal: PaperSignal,
    oms_order_id: UUID | None,
    approval_mode: str,
    intraday_leverage_enabled: bool,
) -> LiveShadowDecision | None:
    """Evaluation as the paper path should call it: never raises, never duplicates.

    Paper execution is the system currently producing results the operator relies
    on. This observer must not be able to interrupt it, so every failure here
    costs one evidence row and nothing else.
    """
    try:
        existing = await session.scalar(
            select(LiveShadowDecision.id).where(LiveShadowDecision.paper_signal_id == signal.id)
        )
        if existing is not None:
            return None
        return await evaluate_live_shadow(
            session,
            settings,
            adapter,
            signal=signal,
            oms_order_id=oms_order_id,
            approval_mode=approval_mode,
            intraday_leverage_enabled=intraday_leverage_enabled,
        )
    except Exception:
        logger.exception("Live shadow evaluation failed for paper signal %s", signal.id)
        return None


@dataclass(frozen=True)
class LiveShadowSummary:
    """What the evidence says so far."""

    evaluated: int
    authorized: int
    refused: int
    unresolved_symbols: int
    authorization_rate_percent: float
    top_refusals: list[dict[str, Any]]


async def summarize_live_shadow(session: AsyncSession, *, limit: int = 5) -> LiveShadowSummary:
    """Aggregate the decisions into the one number that matters, and why not.

    The authorisation rate is the headline: if the live path would have refused a
    third of the trades the paper account took, the paper results do not describe
    the live system.
    """
    evaluated = int(await session.scalar(select(func.count(LiveShadowDecision.id))) or 0)
    authorized = int(
        await session.scalar(select(func.count(LiveShadowDecision.id)).where(LiveShadowDecision.authorized.is_(True)))
        or 0
    )
    unresolved = int(
        await session.scalar(
            select(func.count(LiveShadowDecision.id)).where(LiveShadowDecision.translation_status != "RESOLVED")
        )
        or 0
    )

    counts: dict[str, int] = {}
    rows = (
        await session.scalars(select(LiveShadowDecision.failed_checks).where(LiveShadowDecision.authorized.is_(False)))
    ).all()
    for failed in rows:
        for key in failed or []:
            counts[str(key)] = counts.get(str(key), 0) + 1

    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return LiveShadowSummary(
        evaluated=evaluated,
        authorized=authorized,
        refused=evaluated - authorized,
        unresolved_symbols=unresolved,
        authorization_rate_percent=round(authorized / evaluated * 100, 2) if evaluated else 0.0,
        top_refusals=[{"check": key, "count": count} for key, count in top],
    )
