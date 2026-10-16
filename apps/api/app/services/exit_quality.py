"""How a closed trade actually left, measured against the plan it was given.

The live record says where every trade ended and nothing says *why*. The reason
the live exit manager computed -- "Target reached at 412.80", "Square-off at
15:00 IST", "Time exit: held for 90 minutes" -- goes to Telegram and to the log
and is never written to a row, so no report can group by it.

That gap hid the most important thing in the first fifteen live trades. Measured
back to gross, the average winner made 1.24R against a 2.00R target and the
average loser lost 0.66R against a 1.00R stop: both ends compressed to about
two thirds of the plan. The reward:risk ratio survived that almost intact
(1.89:1 against 2.0:1) so nothing on any screen looked wrong -- but the fixed
per-trade charge is a larger share of a smaller R, and the win rate the account
needs went from 37.5% to 41.3% because of it. The trades were being cut short,
and the only visible symptom was a break-even figure nobody could explain.

So this derives the shape of an exit from what *is* recorded: the fill, the
planned stop, the planned target and the clock.

**Everything here is inference and is labelled as inference.** A trade that
exited at its target price is reported as having reached its target; it is not
proof that the target order is what filled. The honest version of the question
-- which order closed this position -- needs the live exit manager to write its
reason down, and that is a change to the live path, not to a report.
"""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from app.services.trade_history import TradeRecord
from app.services.trading_calendar import MARKET_TIMEZONE

# What closed a trade, as far as the prices and the clock can say.
TARGET = "TARGET"
STOP = "STOP"
SQUARE_OFF = "SQUARE_OFF"
EARLY = "EARLY"
UNKNOWN = "UNKNOWN"

ORDER = (TARGET, STOP, SQUARE_OFF, EARLY, UNKNOWN)

LABELS = {
    TARGET: "reached the planned target",
    STOP: "the planned stop was hit",
    SQUARE_OFF: "closed by the day's deadline, wherever it happened to be",
    EARLY: "left before either level, and not on the clock",
    UNKNOWN: "no fill or no risk recorded, so the shape cannot be measured",
}

# Within 2% of a level counts as that level. A stop fills a tick or two past its
# trigger and a target fills a tick or two short of its limit; demanding the
# exact figure would file most real exits under "left early" and bury the thing
# this exists to find.
AT_LEVEL = Decimal("0.98")

# How close to the deadline still counts as the deadline. The exit sweep runs
# once a minute and a market order takes a moment to come back, so an exit the
# square-off caused lands a minute or three after it.
DEADLINE_MINUTES = 4


@dataclass(frozen=True)
class ExitShape:
    """One closed trade, in units of the risk it was given."""

    session_date: object
    script_name: str
    strategy: str
    side: str
    # What the market gave, before charges. Net R is already on the record; this
    # is the gross figure, because the question here is where price went, and
    # subtracting a cost from it would mix two different measurements.
    gross_r: Decimal | None
    planned_rr: Decimal | None
    # gross_r as a fraction of the reward the plan asked for. 1.0 is the target
    # reached exactly; 0.5 is half of it taken; negative is a loss.
    reach: Decimal | None
    closed_at: time | None
    verdict: str

    @property
    def is_win(self) -> bool:
        return self.gross_r is not None and self.gross_r > 0


def _signed_move(side: str, entry: Decimal, price: Decimal) -> Decimal:
    return (price - entry) if side.upper() == "LONG" else (entry - price)


def shape(record: TradeRecord, *, square_off: time | None) -> ExitShape:
    """One trade's exit, measured in R against its own plan."""
    entry, exit_price = record.entry_price, record.exit_price
    risk_per_unit = None if entry is None else abs(entry - record.stop_price)
    reward_per_unit = None if entry is None else abs(record.target_price - entry)
    closed = None if record.closed_at is None else record.closed_at.astimezone(MARKET_TIMEZONE).time()

    gross_r = planned_rr = reach = None
    if entry is not None and exit_price is not None and risk_per_unit and risk_per_unit > 0:
        gross_r = (_signed_move(record.side, entry, exit_price) / risk_per_unit).quantize(Decimal("0.01"))
        if reward_per_unit and reward_per_unit > 0:
            planned_rr = (reward_per_unit / risk_per_unit).quantize(Decimal("0.01"))
            reach = (gross_r / planned_rr).quantize(Decimal("0.01"))

    return ExitShape(
        session_date=record.session_date,
        script_name=record.script_name,
        strategy=(record.strategy_version or "").split("@")[0] or "unknown",
        side=record.side,
        gross_r=gross_r,
        planned_rr=planned_rr,
        reach=reach,
        closed_at=closed,
        verdict=_verdict(gross_r, planned_rr, closed, square_off),
    )


def _verdict(gross_r: Decimal | None, planned_rr: Decimal | None, closed: time | None, square_off: time | None) -> str:
    """Price first, then the clock.

    A trade that reached its target at 14:58 reached its target; the deadline
    two minutes later did not close it. So the levels are tested before the
    clock, and only an exit that is at neither level is attributed to the
    deadline.
    """
    if gross_r is None:
        return UNKNOWN
    if planned_rr is not None and gross_r >= planned_rr * AT_LEVEL:
        return TARGET
    if gross_r <= -AT_LEVEL:
        return STOP
    if closed is not None and square_off is not None and _within_deadline(closed, square_off):
        return SQUARE_OFF
    return EARLY


def _within_deadline(closed: time, square_off: time) -> bool:
    minutes = closed.hour * 60 + closed.minute
    deadline = square_off.hour * 60 + square_off.minute
    return minutes >= deadline - DEADLINE_MINUTES


def tally(shapes: list[ExitShape]) -> dict[str, int]:
    """How many trades left each way, in report order and without empty rows."""
    counts = {key: 0 for key in ORDER}
    for item in shapes:
        counts[item.verdict] += 1
    return {key: value for key, value in counts.items() if value}


def average_reach(shapes: list[ExitShape], *, winners_only: bool = True) -> Decimal | None:
    """How much of the planned reward the trades actually collected.

    Winners only by default, because that is the question: a strategy whose
    winners collect 60% of their target is not the strategy that was measured,
    whatever its win rate. Losers have their own figure and mixing the two
    averages a fraction of a reward with a fraction of a risk.
    """
    values = [item.reach for item in shapes if item.reach is not None and (item.is_win or not winners_only)]
    if not values:
        return None
    return (sum(values, start=Decimal("0")) / len(values)).quantize(Decimal("0.01"))


def average_gross_r(shapes: list[ExitShape], *, wins: bool) -> Decimal | None:
    values = [item.gross_r for item in shapes if item.gross_r is not None and (item.gross_r > 0) == wins]
    if not values:
        return None
    return (sum(values, start=Decimal("0")) / len(values)).quantize(Decimal("0.01"))
