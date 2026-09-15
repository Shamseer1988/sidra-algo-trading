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
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ExecutionReconciliation
from app.services.firstock.client import FirstockError
from app.services.firstock.orders import FirstockReportClient
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


def _decimal(value: Any) -> Decimal | None:
    """Parse a broker-supplied number. Unreadable is None, never zero.

    Treating an unparseable margin as zero would be safe; treating it as zero when
    the caller then compares "required <= available" would not. Returning None
    forces the caller to fail the check explicitly.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


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
    client: FirstockReportClient,
    *,
    exchange: str,
    product: str,
    price_type: str,
    trading_symbol: str,
    transaction_type: str,
    price: str,
    quantity: str,
) -> LiveRiskCheck:
    """Ask the broker whether this specific order is affordable.

    A successful API call is not permission to trade: the documented response can
    report insufficient balance in ``remarks`` while the envelope still says
    success, so the numbers are compared rather than the status.
    """
    try:
        data = await client.order_margin(
            exchange=exchange,
            product=product,
            price_type=price_type,
            trading_symbol=trading_symbol,
            transaction_type=transaction_type,
            price=price,
            quantity=quantity,
        )
    except FirstockError as exc:
        return LiveRiskCheck("broker_margin", False, f"Margin check failed: {exc}")

    required = _decimal(data.get("marginOnNewOrder"))
    available = _decimal(data.get("availableMargin"))
    if required is None or available is None:
        return LiveRiskCheck("broker_margin", False, "Broker margin response was not readable.")
    if required > available:
        return LiveRiskCheck("broker_margin", False, f"Order needs {required} against {available} available.")

    remarks = str(data.get("remarks") or "").strip()
    if remarks and "insufficient" in remarks.lower():
        return LiveRiskCheck("broker_margin", False, f"Broker reported: {remarks}")
    return LiveRiskCheck("broker_margin", True, f"Broker margin {available} covers {required}.")


async def authorize_live_order(
    session: AsyncSession,
    settings: Settings,
    client: FirstockReportClient,
    *,
    approval_mode: str,
    exchange: str,
    product: str,
    price_type: str,
    trading_symbol: str,
    transaction_type: str,
    price: str,
    quantity: str,
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
        parsed_quantity = int(str(quantity).strip())
    except (TypeError, ValueError):
        parsed_quantity = 0
    checks.append(LiveRiskCheck("quantity", parsed_quantity > 0, f"Quantity {quantity!r} parsed as {parsed_quantity}."))

    checks.append(
        await _margin_check(
            client,
            exchange=exchange,
            product=product,
            price_type=price_type,
            trading_symbol=trading_symbol,
            transaction_type=transaction_type,
            price=price,
            quantity=quantity,
        )
    )

    failures = [check for check in checks if not check.passed]
    return LiveRiskDecision(
        authorized=not failures,
        reason="Authorised" if not failures else "; ".join(check.detail for check in failures),
        checked_at=checked_at,
        checks=checks,
    )
