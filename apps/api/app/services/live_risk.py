"""Per-order authorisation for live submission.

Phase 2 of the live-execution layer, and the last thing that says no before an
order would reach a broker. It authorises nothing today: the readiness gates in
``live_readiness`` are still closed, and this engine requires them.

**This module must never import the paper risk engine, and the paper engine must
never import this one.** They answer superficially similar questions and the
temptation to share code is real, but a bug in paper sizing that merely produces
a wrong journal entry becomes a wrong live order the moment the code path is
shared. Duplication is the cheaper mistake here.

The engine is deny-by-default in structure, not merely by intent. It collects a
list of checks and authorises only when every one of them passed. A check that
raises, a check that is skipped, or a new check added without wiring it into the
result all produce a refusal rather than an approval.

What it deliberately does not do: size the order, choose a price, or submit
anything. It answers one question — may this specific order be sent right now —
and the answer defaults to no.

The daily profit target and loss limit are checked here too, against the
broker's own position book rather than the paper ledger. They are the same two
numbers an operator set for paper, and they are deliberately measured from a
different source: paper P&L is a simulation, and refusing a real order because a
simulation had a good morning is a refusal nobody could act on.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting, ExecutionReconciliation
from app.services import daily_limits
from app.services.broker_adapter import BrokerAdapter, BrokerOrder, BrokerOrderDescription
from app.services.live_readiness import inspect_live_readiness

# A reconciliation older than this tells us about an account that may since have
# changed. Short enough that a stale pass cannot authorise a whole session.
RECONCILIATION_MAX_AGE = timedelta(minutes=15)

APPROVAL_MODES_PERMITTING_SUBMISSION = frozenset({"TELEGRAM_APPROVAL", "AUTOMATIC"})


@dataclass(frozen=True)
class LiveRiskCheck:
    key: str
    passed: bool
    detail: str
    # Structured facts behind the verdict, for callers that need the numbers
    # rather than the sentence. Parsing them back out of ``detail`` would be a
    # fragility worth avoiding: the prose is for operators, this is for code.
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveRiskDecision:
    authorized: bool
    reason: str
    checked_at: datetime
    checks: list[LiveRiskCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[LiveRiskCheck]:
        return [check for check in self.checks if not check.passed]

    def snapshot(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
            "checks": [asdict(check) for check in self.checks],
        }


async def _reconciliation_check(session: AsyncSession) -> LiveRiskCheck:
    """Require a recent live reconciliation that cleared the account for trading."""
    record = await session.scalar(
        select(ExecutionReconciliation)
        .where(ExecutionReconciliation.mode == "LIVE")
        .order_by(ExecutionReconciliation.created_at.desc())
        .limit(1)
    )
    if record is None:
        return LiveRiskCheck("reconciliation", False, "No live reconciliation has ever been recorded.")
    if not record.safe_to_trade:
        return LiveRiskCheck("reconciliation", False, f"Latest reconciliation blocked trading: {record.detail}")

    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created_at
    if age > RECONCILIATION_MAX_AGE:
        return LiveRiskCheck(
            "reconciliation",
            False,
            f"Latest reconciliation is {int(age.total_seconds() // 60)} minutes old; re-run before trading.",
        )
    return LiveRiskCheck("reconciliation", True, "Broker and local state agreed within the freshness window.")


async def _margin_check(
    adapter: BrokerAdapter,
    order: BrokerOrder,
    description: BrokerOrderDescription,
) -> LiveRiskCheck:
    """Ask the broker whether this specific order is affordable.

    The two brokers answer this differently — Firstock in one call that can
    refuse inside a success envelope, Upstox in two — so the quote is obtained
    through the adapter and only compared here. What stays in this module is the
    part that must not vary: unreadable is a refusal, and a quote that was never
    obtained is never treated as one that passed.
    """
    quote = await adapter.order_margin(order, description)
    numbers = {
        "required": str(quote.required) if quote.required is not None else "",
        "available": str(quote.available) if quote.available is not None else "",
    }
    return LiveRiskCheck("broker_margin", quote.affordable, quote.detail, numbers)


async def _symbol_check(description: BrokerOrderDescription) -> LiveRiskCheck:
    """Can this instrument be named at this broker at all.

    Separate from the margin check so a refusal says which of the two failed.
    Folded together, an unmappable symbol would be reported as a margin problem
    and somebody would go and look at the account balance.
    """
    if description.resolved:
        return LiveRiskCheck(
            "instrument", True, f"Instrument resolves to {description.symbol} on {description.exchange}."
        )
    return LiveRiskCheck("instrument", False, description.detail or "Instrument could not be named at this broker.")


async def _daily_limit_check(
    session: AsyncSession,
    adapter: BrokerAdapter,
    *,
    record: bool,
) -> LiveRiskCheck:
    """Has the broker account already made or lost what the operator allowed.

    The P&L comes from the broker's own position book, never from the paper
    ledger. They are different numbers about different money, and a live order
    refused because a simulation had a good morning is a refusal nobody could
    act on.

    Unreadable P&L refuses. This is the same rule the margin check follows and
    it matters more here: a position whose P&L cannot be parsed is a day whose
    total cannot be bounded, and the limit exists precisely to bound it. A
    broker that will not tell us is indistinguishable, from here, from a broker
    telling us we have lost too much.

    ``record`` is False for the shadow evaluator, which submits nothing. It
    still reads the latch, so its evidence shows the gate that would have
    refused, but a path that cannot place an order must not be able to close the
    live day either.
    """
    # Imported inside the function: the settings route imports live modules for
    # its own types, and a module-level import here would close the cycle.
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

    setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    if not (controls.daily_loss_limit or controls.daily_profit_target):
        return LiveRiskCheck("daily_limit", True, "No daily profit target or loss limit is configured.")

    session_date = datetime.now(UTC).date()
    halt = await daily_limits.existing_halt(session, session_date, daily_limits.LIVE)
    if halt is not None:
        # Read before the broker is asked: the day is already over, and a
        # network call cannot change that.
        return LiveRiskCheck(
            "daily_limit",
            False,
            f"{halt.reason} at {halt.session_pnl}; live trading is finished for {session_date}.",
            {"session_pnl": str(halt.session_pnl), "reason": halt.reason},
        )

    try:
        positions = await adapter.normalised_positions()
    except Exception as exc:
        return LiveRiskCheck("daily_limit", False, f"Broker positions could not be read: {exc}")

    unreadable = [item.symbol for item in positions if item.day_pnl is None]
    if unreadable:
        return LiveRiskCheck(
            "daily_limit",
            False,
            f"Broker reported unreadable P&L for {', '.join(sorted(unreadable))}; "
            "the day's total cannot be established.",
        )

    session_pnl = sum((item.day_pnl or Decimal("0") for item in positions), start=Decimal("0"))
    verdict = await daily_limits.verdict_for(
        session,
        session_date,
        daily_limits.LIVE,
        session_pnl=session_pnl,
        controls=controls,
    )
    numbers = {"session_pnl": str(session_pnl)}
    if not verdict.halted:
        return LiveRiskCheck("daily_limit", True, f"Broker P&L {session_pnl} is inside the day's limits.", numbers)

    if record:
        await daily_limits.record_halt(session, session_date, daily_limits.LIVE, verdict)
        await session.commit()
    return LiveRiskCheck("daily_limit", False, f"{verdict.reason} at {session_pnl}.", numbers)


async def authorize_live_order(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    *,
    approval_mode: str,
    order: BrokerOrder,
    description: BrokerOrderDescription,
    record_halt: bool = False,
) -> LiveRiskDecision:
    """Decide whether one specific order may be submitted right now.

    Every check runs even after one fails, so an operator sees the full picture
    rather than fixing objections one at a time. Authorisation requires all of
    them; the result is computed from the collected checks rather than tracked in
    a flag that a later edit could forget to clear.
    """
    checked_at = datetime.now(UTC)
    checks: list[LiveRiskCheck] = []

    readiness = await inspect_live_readiness(session, settings)
    checks.append(
        LiveRiskCheck(
            "live_readiness",
            readiness.overall_ready,
            "All live readiness gates passed."
            if readiness.overall_ready
            else "Blocked by: " + ", ".join(gate.key for gate in readiness.gates if not gate.passed),
        )
    )

    normalized_mode = (approval_mode or "").strip().upper()
    checks.append(
        LiveRiskCheck(
            "approval_mode",
            normalized_mode in APPROVAL_MODES_PERMITTING_SUBMISSION,
            f"Execution approval mode is {normalized_mode or 'unset'}.",
        )
    )

    checks.append(await _reconciliation_check(session))

    # Parsed defensively: this engine exists to refuse, so it must not raise.
    try:
        parsed_quantity = int(str(order.quantity).strip())
    except (TypeError, ValueError):
        parsed_quantity = 0
    checks.append(
        LiveRiskCheck("quantity", parsed_quantity > 0, f"Quantity {order.quantity!r} parsed as {parsed_quantity}.")
    )

    checks.append(await _symbol_check(description))
    checks.append(await _daily_limit_check(session, adapter, record=record_halt))
    checks.append(await _margin_check(adapter, order, description))

    failures = [check for check in checks if not check.passed]
    return LiveRiskDecision(
        authorized=not failures,
        reason="Authorised" if not failures else "; ".join(check.detail for check in failures),
        checked_at=checked_at,
        checks=checks,
    )
