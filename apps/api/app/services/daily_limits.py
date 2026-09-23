"""When the day's money says stop.

Two limits, one rule: a session that has made ``daily_profit_target`` or lost
``daily_loss_limit`` is finished trading. Zero disables either.

**The verdict latches.** It is recorded against the session date the first time
it is reached, and every later check reads the record rather than recomputing.
That is the difference between a stop and a filter, and the reason it matters is
specific: session P&L includes open positions, so a winner showing +2,200 can
close at +800. Recomputing would re-open a day that had already finished, and
the operator who was told "target reached" would find the system trading again
an hour later. A stop that can un-trip is not a stop.

The losing side does not need the latch for the same reason — losses do not
un-lose — but it gets one anyway, because a rule with an exception is a rule
somebody has to remember.

**Two places evaluate this, and they must agree.** The risk engine checks it
when a signal wants to open a position; paper execution checks it on every
completed candle, because a limit is usually breached by a price moving rather
than by a trade being taken, and nothing would notice until the next signal
arrived — which on a halted day never comes. The evaluation lives here so there
is one implementation rather than two that drift.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PaperPosition, PaperSessionHalt

LOSS_LIMIT_REACHED = "Daily loss limit reached"
PROFIT_TARGET_REACHED = "Daily profit target reached"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


@dataclass(frozen=True)
class DailyVerdict:
    """The day's money, and whether it has anything left to say."""

    session_pnl: Decimal
    reason: str | None

    @property
    def halted(self) -> bool:
        return self.reason is not None


def session_pnl_of(positions: list[PaperPosition]) -> Decimal:
    """Realised plus unrealised minus costs, across every position of the day.

    Open positions count. A session sitting on a large unrealised loss has lost
    the money whether or not the trade has closed, and a limit that waits for
    the booking is a limit that lets the next trade through at exactly the wrong
    moment.
    """
    return sum((_decimal(item.total_pnl) for item in positions), start=Decimal("0"))


def evaluate(session_pnl: Decimal, controls: Any) -> str | None:
    """Which limit, if either, this P&L has reached.

    The loss limit is checked first: a day that is down cannot also be a day
    that has finished winning, and reporting it as the latter would be actively
    misleading.
    """
    loss_limit = _decimal(getattr(controls, "daily_loss_limit", 0) or 0)
    profit_target = _decimal(getattr(controls, "daily_profit_target", 0) or 0)
    if loss_limit > 0 and session_pnl <= -loss_limit:
        return LOSS_LIMIT_REACHED
    if profit_target > 0 and session_pnl >= profit_target:
        return PROFIT_TARGET_REACHED
    return None


async def existing_halt(session: AsyncSession, session_date: date) -> PaperSessionHalt | None:
    return await session.scalar(select(PaperSessionHalt).where(PaperSessionHalt.session_date == session_date))


async def positions_for(session: AsyncSession, session_date: date) -> list[PaperPosition]:
    """Every position the session produced, closed ones included."""
    rows = await session.scalars(select(PaperPosition).where(PaperPosition.session_date == session_date))
    return list(rows.all())


async def verdict_for(session: AsyncSession, session_date: date, controls: Any) -> DailyVerdict:
    """The day's standing: a recorded halt if there is one, else a fresh look.

    A recorded halt wins over the current numbers. That is the latch.
    """
    halt = await existing_halt(session, session_date)
    if halt is not None:
        return DailyVerdict(session_pnl=_decimal(halt.session_pnl), reason=halt.reason)
    pnl = session_pnl_of(await positions_for(session, session_date))
    return DailyVerdict(session_pnl=pnl, reason=evaluate(pnl, controls))


async def record_halt(session: AsyncSession, session_date: date, verdict: DailyVerdict) -> PaperSessionHalt | None:
    """Write the latch once. The caller commits.

    Returns None when the day was already halted, so a caller can tell the
    transition from the steady state — closing positions is worth doing once
    and pointless every candle afterwards.
    """
    if verdict.reason is None:
        return None
    if await existing_halt(session, session_date) is not None:
        return None
    halt = PaperSessionHalt(
        session_date=session_date,
        reason=verdict.reason,
        session_pnl=verdict.session_pnl,
    )
    session.add(halt)
    await session.flush()
    return halt
