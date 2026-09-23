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

**Paper and live are separate days.** They halt independently, under the same
two numbers, from different sources: paper from its own position ledger, live
from the broker's. ``mode`` keeps them apart, because a paper day that hits its
target says nothing about the money in the Upstox account, and stopping real
trading on the strength of a simulation would be a refusal nobody could explain.

**This module deliberately contains no P&L computation.** ``live_risk`` has a
standing rule against sharing code with the paper risk engine — a bug in paper
sizing that merely writes a wrong journal entry becomes a wrong live order the
moment the path is shared. What is shared here is narrower than that and does
not carry the same risk: the operator's two numbers, the comparison against
them, and the record of the verdict. Each caller works out its own P&L from its
own source and passes the figure in. Duplicating "is 2,000 at least 2,000" would
not make anything safer; it would only create two places for the two modes to
start disagreeing about what the operator asked for.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SessionHalt

LOSS_LIMIT_REACHED = "Daily loss limit reached"
PROFIT_TARGET_REACHED = "Daily profit target reached"

PAPER = "PAPER"
LIVE = "LIVE"


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


async def existing_halt(session: AsyncSession, session_date: date, mode: str) -> SessionHalt | None:
    return await session.scalar(
        select(SessionHalt).where(SessionHalt.session_date == session_date, SessionHalt.mode == mode)
    )


async def verdict_for(
    session: AsyncSession,
    session_date: date,
    mode: str,
    *,
    session_pnl: Decimal,
    controls: Any,
) -> DailyVerdict:
    """The day's standing: a recorded halt if there is one, else a fresh look.

    A recorded halt wins over the figure passed in. That is the latch.
    """
    halt = await existing_halt(session, session_date, mode)
    if halt is not None:
        return DailyVerdict(session_pnl=_decimal(halt.session_pnl), reason=halt.reason)
    return DailyVerdict(session_pnl=session_pnl, reason=evaluate(session_pnl, controls))


async def record_halt(
    session: AsyncSession,
    session_date: date,
    mode: str,
    verdict: DailyVerdict,
) -> SessionHalt | None:
    """Write the latch once. The caller commits.

    Returns None when the day was already halted, so a caller can tell the
    transition from the steady state — closing positions is worth doing once
    and pointless every candle afterwards.
    """
    if verdict.reason is None:
        return None
    if await existing_halt(session, session_date, mode) is not None:
        return None
    halt = SessionHalt(
        session_date=session_date,
        mode=mode,
        reason=verdict.reason,
        session_pnl=verdict.session_pnl,
    )
    session.add(halt)
    await session.flush()
    return halt
