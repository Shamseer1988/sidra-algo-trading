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

For the same reason, the three remaining portfolio limits — concurrent open
positions, open exposure, and the day's risk allocation — are enforced here from
live data: open positions and exposure from the broker's own book, allocation
from the risk the day's live submissions were sized from. They were previously
enforced only by the paper risk engine, which meant the live account was bounded
by a simulation. The two agree while nothing diverges and are unrelated the
moment anything does: a paper position for an order the broker refused held a
live slot, and a live fill the paper engine never saw held none.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting, ExecutionReconciliation, LiveOrderSubmission
from app.services import daily_limits
from app.services.broker_adapter import BrokerAdapter, BrokerOrder, BrokerOrderDescription
from app.services.live_readiness import inspect_live_readiness
from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc

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
    controls: Any,
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


async def live_risk_allocated_today(session: AsyncSession, session_date: date) -> tuple[Decimal, int]:
    """Risk committed to the live account today, and how many trades carry it.

    Read from ``live_order_submissions`` and nothing else. No paper position,
    no paper fill, no paper verdict, and -- deliberately -- no join into
    ``paper_signals`` either: the figure is written onto the live row when the
    order is prepared, so the live account's real budget never depends on a
    simulation's table. ``planned_risk`` is what the order was *sized to*
    risk, which is why an exit and a protective stop carry none.

    Counted per trade rather than per row. One trade writes three submissions
    -- the entry, the protective stop behind it, and the exit that closes it --
    and charging a single round trip three times would close the day after a
    third of it. Rows sharing a signal are therefore one trade, at the largest
    risk any of them declared; a row with no signal behind it is its own trade,
    because nothing links it to another.
    """
    start, end = session_bounds_utc(session_date)
    rows = (
        await session.execute(
            select(
                LiveOrderSubmission.id,
                LiveOrderSubmission.paper_signal_id,
                LiveOrderSubmission.planned_risk,
            ).where(
                LiveOrderSubmission.created_at >= start,
                LiveOrderSubmission.created_at < end,
                LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
                LiveOrderSubmission.planned_risk.is_not(None),
            )
        )
    ).all()
    per_trade: dict[Any, Decimal] = {}
    for row_id, signal_id, risk in rows:
        key = signal_id if signal_id is not None else row_id
        declared = Decimal(str(risk))
        per_trade[key] = max(per_trade.get(key, declared), declared)
    return sum(per_trade.values(), start=Decimal("0")), len(per_trade)


_CONTROLS_UNREADABLE = (
    "The saved trading controls could not be read, so the day's limits and the live account's own "
    "position, exposure and daily-allocation limits cannot be established."
)


async def _live_controls(session: AsyncSession) -> Any | None:
    """The saved trading controls, or None if they cannot be read.

    Imported inside the function for the same reason the daily-limit check does
    it: the settings route imports this module back.

    A stored value that no longer validates must not make this engine raise --
    it refuses instead, which is the behaviour every other check here has. The
    defaults are used only when nothing has ever been saved; a row that exists
    and fails validation is a refusal rather than a quiet fall back to numbers
    the operator never chose.
    """
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

    try:
        setting = await session.get(ApplicationSetting, TRADING_KEY)
        return TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    except Exception:  # noqa: BLE001 - this engine refuses, it does not raise
        return None


async def _live_allocation_check(session: AsyncSession, controls: Any, candidate_risk: Decimal | None) -> LiveRiskCheck:
    """Whether the day's live risk budget has room for this order.

    maximum_daily_risk_percent was enforced only by the paper risk engine,
    which meant the live account's budget was policed by a simulation: a day
    whose paper ledger and live account had drifted apart was bounded by the
    wrong one of the two. This reads live submissions.
    """
    budget = Decimal(str(controls.account_capital)) * Decimal(str(controls.maximum_daily_risk_percent)) / Decimal("100")
    if budget <= 0:
        return LiveRiskCheck("live_allocation", True, "No daily risk allocation limit is configured.")
    allocated, trades = await live_risk_allocated_today(session, datetime.now(UTC).date())
    wanted = candidate_risk or Decimal("0")
    numbers = {"allocated": str(allocated), "budget": str(budget), "candidate": str(wanted), "trades": trades}
    if allocated + wanted > budget:
        return LiveRiskCheck(
            "live_allocation",
            False,
            f"The day's live risk budget is spent: {allocated} of {budget} allocated across {trades} "
            f"live trade(s), and this order needs {wanted}.",
            numbers,
        )
    return LiveRiskCheck(
        "live_allocation",
        True,
        f"{allocated} of {budget} of the day's live risk is allocated across {trades} live trade(s).",
        numbers,
    )


async def _live_position_check(
    adapter: BrokerAdapter, controls: Any, order: BrokerOrder, description: BrokerOrderDescription | None
) -> LiveRiskCheck:
    """Concurrent live positions and live exposure, read from the account.

    Both limits existed only in the paper risk engine, so the live account was
    capped by how many *simulated* positions were open. Those two agree while
    nothing diverges and are unrelated the moment anything does -- a paper
    position for an order the broker refused held a live slot, and a live fill
    the paper engine never saw held none.

    Fails closed on an unreadable book, like the daily limit above: a position
    count that cannot be established is not a reason to send another order.
    An order that closes or reduces an existing position is exempt, because
    refusing those would trap a position open with no way out.
    """
    limit = int(controls.maximum_open_positions or 0)
    exposure_percent = Decimal(str(controls.maximum_open_exposure_percent or 0))
    if limit <= 0 and exposure_percent <= 0:
        return LiveRiskCheck("live_positions", True, "No concurrent position or exposure limit is configured.")

    try:
        positions = await adapter.normalised_positions()
    except Exception as exc:  # noqa: BLE001 - this engine refuses, it does not raise
        return LiveRiskCheck("live_positions", False, f"Broker positions could not be read: {exc}")

    unreadable = [item.symbol for item in positions if item.net_quantity is None]
    if unreadable:
        return LiveRiskCheck(
            "live_positions",
            False,
            f"Broker reported an unreadable quantity for {', '.join(sorted(unreadable))}; "
            "the open position count cannot be established.",
        )

    open_rows = [item for item in positions if item.net_quantity and item.net_quantity != 0]
    # The canonical token from the order, and the broker-facing name from the
    # description -- which is where it lives: a BrokerOrder has no symbol and a
    # BrokerOrderDescription has no token, so taking both from one of them
    # matches on nothing.
    token = getattr(order, "instrument_token", None)
    symbol = getattr(description, "symbol", None)
    # An order against a token we already hold is adding to, reducing or
    # closing that position, not opening another one.
    mine = [item for item in open_rows if item.identifies(token, symbol)]
    already_held = bool(mine)
    # Opposite to the position it names, so it can only make the book smaller.
    # Exempt from the exposure ceiling for the reason exits bypass these gates
    # entirely: a limit that refuses the order which would bring the account
    # back inside it traps the position it was meant to protect against.
    side = str(getattr(order, "side", "") or "").strip().upper()
    reduces = any(
        (item.net_quantity > 0 and side in {"SELL", "S"}) or (item.net_quantity < 0 and side in {"BUY", "B"})
        for item in mine
        if item.net_quantity is not None
    )

    held = len(open_rows)
    exposure = sum(
        (
            abs(item.net_quantity or Decimal("0")) * Decimal(str(item.last_price or item.average_price or 0))
            for item in open_rows
        ),
        start=Decimal("0"),
    )
    numbers = {"open_positions": held, "limit": limit, "exposure": str(exposure)}

    if limit > 0 and not already_held and held >= limit:
        return LiveRiskCheck(
            "live_positions",
            False,
            f"{held} live position(s) are already open and the limit is {limit}: "
            f"{', '.join(sorted(item.symbol for item in open_rows))}.",
            numbers,
        )

    if exposure_percent > 0:
        leverage = (
            Decimal(str(controls.intraday_leverage_multiplier))
            if getattr(controls, "intraday_leverage_enabled", False)
            else Decimal("1")
        )
        ceiling = Decimal(str(controls.account_capital)) * exposure_percent * leverage / Decimal("100")
        numbers["exposure_ceiling"] = str(ceiling)
        if ceiling > 0 and exposure > ceiling and not reduces:
            return LiveRiskCheck(
                "live_positions",
                False,
                f"Live exposure {exposure} already exceeds the ceiling of {ceiling}.",
                numbers,
            )

    return LiveRiskCheck(
        "live_positions",
        True,
        f"{held} of {limit} live position(s) open, exposure {exposure}.",
        numbers,
    )


async def authorize_live_order(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    *,
    approval_mode: str,
    order: BrokerOrder,
    description: BrokerOrderDescription,
    record_halt: bool = False,
    candidate_risk: Decimal | None = None,
) -> LiveRiskDecision:
    """Decide whether one specific order may be submitted right now.

    Every check runs even after one fails, so an operator sees the full picture
    rather than fixing objections one at a time. Authorisation requires all of
    them; the result is computed from the collected checks rather than tracked in
    a flag that a later edit could forget to clear.

    ``candidate_risk`` is what this order plans to risk, so the day's live
    allocation can be checked with it included rather than only after it is
    spent. Omitted, the budget is still enforced against what is already
    allocated -- a caller that cannot say what the order risks does not get to
    skip the limit, it only loses the reservation for its own order.
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
    checks.append(await _margin_check(adapter, order, description))

    # The operator's saved numbers, loaded once so every limit below answers to
    # the same ones. Unreadable refuses all four rather than raising out of an
    # engine whose contract is to say no.
    controls = await _live_controls(session)
    if controls is None:
        for key in ("daily_limit", "live_allocation", "live_positions"):
            checks.append(LiveRiskCheck(key, False, _CONTROLS_UNREADABLE))
    else:
        checks.append(await _daily_limit_check(session, adapter, controls, record=record_halt))
        # The live account's own limits, from the live account. These three were
        # enforced only by the paper risk engine, so the real money was bounded
        # by a simulation: the two agree while nothing diverges and are
        # unrelated the moment anything does.
        checks.append(await _live_allocation_check(session, controls, candidate_risk))
        checks.append(await _live_position_check(adapter, controls, order, description))

    failures = [check for check in checks if not check.passed]
    return LiveRiskDecision(
        authorized=not failures,
        reason="Authorised" if not failures else "; ".join(check.detail for check in failures),
        checked_at=checked_at,
        checks=checks,
    )
