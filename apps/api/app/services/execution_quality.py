"""The three numbers that decide whether a strategy is worth running.

Everything needed for these is already recorded and none of it is reported. The
record says what was traded; this says whether it was traded *well*, which is a
different question and the one an operator has to answer before leaving a system
unattended.

**Fill rate.** Entries are sent as capped limit orders, so some never fill. That
is the design working — a fill at a materially worse price is a different trade
— but it is also a hidden strategy parameter. A strategy that looks poor because
it misses its best entries and a strategy that is poor are indistinguishable
until somebody counts.

**Slippage.** Two kinds, and they answer different questions. Entry slippage is
the fill against the price the signal planned, which is what the entry cap
exists to bound. Trade slippage is the broker's gross against the simulator's,
which is what the whole round trip actually cost against what the journal
assumed.

**Expectancy, net of what the broker charged.** Gross expectancy flatters every
strategy by exactly the amount it costs to run. The per-trade charge in this
system is an estimate from the published rate card and always will be, so the
drag is measured from the broker's own day totals where those exist and is
labelled as an estimate where they do not.

**And the sample.** Nine trades is not evidence of an edge; it is evidence about
execution. Every figure here carries the count it was computed from, and the
report states in words what the sample can and cannot support, because a number
with no sample beside it is read as a finding.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission, PaperSignal, RiskReservation
from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc
from app.services.trade_history import DaySummary, TradeRecord

# What a sample of live trades can support. Not a formula — a working rule a
# trader would recognise, stated so a reader is not left to guess which of the
# three regimes the numbers in front of them belong to.
TOO_FEW = 30
INDICATIVE = 100

ENTRY_TYPES = frozenset({"LIMIT", "MARKET"})


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _mean(values: list[Decimal]) -> Decimal | None:
    return (sum(values, start=Decimal("0")) / len(values)).quantize(Decimal("0.01")) if values else None


@dataclass
class Funnel:
    """Where setups go between being found and being money.

    Each step is the subset of the one above it, so a drop between two rows is a
    question with one answer rather than several.
    """

    signals: int = 0
    accepted: int = 0
    refused: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    sent: int = 0
    filled: int = 0
    # Live entries we hold no fill record for. Counted apart from "did not
    # fill", because not knowing and knowing it missed are different facts and
    # averaging them would invent a fill rate.
    unknown: int = 0

    @property
    def fill_rate_percent(self) -> Decimal | None:
        decided = self.sent - self.unknown
        if decided <= 0:
            return None
        return (Decimal(self.filled) * 100 / Decimal(decided)).quantize(Decimal("0.1"))

    @property
    def acceptance_percent(self) -> Decimal | None:
        if not self.signals:
            return None
        return (Decimal(self.accepted) * 100 / Decimal(self.signals)).quantize(Decimal("0.1"))


@dataclass
class Slippage:
    """What the fills cost against what was planned and what was modelled."""

    entry_trades: int = 0
    entry_total: Decimal = Decimal("0")
    entry_average: Decimal | None = None
    entry_worst: Decimal | None = None
    trade_trades: int = 0
    trade_total: Decimal = Decimal("0")
    trade_average: Decimal | None = None
    trade_worst: Decimal | None = None


@dataclass
class Expectancy:
    """What one trade is worth, on average, after costs."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    win_rate_percent: Decimal | None = None
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    gross_per_trade: Decimal | None = None
    net_per_trade: Decimal | None = None
    average_r: Decimal | None = None
    # The win rate this profile needs simply to break even. The single most
    # useful number on the screen: compared against the real win rate it says
    # whether the edge covers its own costs.
    break_even_win_rate_percent: Decimal | None = None


@dataclass
class Charges:
    """What trading cost, from the broker where it has said."""

    gross: Decimal = Decimal("0")
    estimated: Decimal = Decimal("0")
    broker: Decimal | None = None
    broker_days: int = 0
    days_pending: int = 0
    per_trade: Decimal | None = None
    percent_of_gross: Decimal | None = None


@dataclass
class StrategyQuality:
    strategy_version: str
    trades: int
    wins: int
    net: Decimal
    net_per_trade: Decimal | None
    average_r: Decimal | None


@dataclass
class QualityReport:
    from_date: date
    to_date: date
    funnel: Funnel
    slippage: Slippage
    expectancy: Expectancy
    charges: Charges
    strategies: list[StrategyQuality] = field(default_factory=list)
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


async def read_funnel(session: AsyncSession, from_date: date, to_date: date) -> Funnel:
    """Signals found, risk's verdict on them, orders sent, orders filled."""
    funnel = Funnel()
    funnel.signals = int(
        await session.scalar(
            select(func.count(PaperSignal.id)).where(
                PaperSignal.session_date >= from_date, PaperSignal.session_date <= to_date
            )
        )
        or 0
    )

    rows = await session.execute(
        select(RiskReservation.status, RiskReservation.decision_reason, func.count(RiskReservation.id))
        .where(RiskReservation.session_date >= from_date, RiskReservation.session_date <= to_date)
        .group_by(RiskReservation.status, RiskReservation.decision_reason)
    )
    for status, reason, count in rows:
        if status == "REJECTED":
            funnel.refused += int(count)
            funnel.refusals[reason] = funnel.refusals.get(reason, 0) + int(count)
        else:
            funnel.accepted += int(count)

    start, _ = session_bounds_utc(from_date)
    _, end = session_bounds_utc(to_date)
    submissions = await session.scalars(
        select(LiveOrderSubmission).where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
        )
    )
    # Entries only. A stop and an exit are the same trade reaching the broker
    # again, and counting them would report a fill rate over three times as many
    # orders as there were trades.
    seen: set = set()
    for row in submissions.all():
        if (row.canonical_order_type or "") not in ENTRY_TYPES:
            continue
        key = row.paper_signal_id or row.id
        if key in seen:
            continue
        seen.add(key)
        funnel.sent += 1
        if row.filled_quantity is None:
            funnel.unknown += 1
        elif row.filled_quantity > 0:
            funnel.filled += 1
    return funnel


async def read_entry_slippage(session: AsyncSession, from_date: date, to_date: date) -> list[Decimal]:
    """Fill price against the price the signal planned, signed so worse is worse.

    The number the entry cap exists to bound, and the one that says whether the
    cap is set sensibly: a cap that is never approached is costing fills for
    nothing, and one that is always reached is not capping anything.
    """
    start, _ = session_bounds_utc(from_date)
    _, end = session_bounds_utc(to_date)
    rows = await session.execute(
        select(LiveOrderSubmission, PaperSignal)
        .join(PaperSignal, PaperSignal.id == LiveOrderSubmission.paper_signal_id)
        .where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            LiveOrderSubmission.average_fill_price.isnot(None),
        )
    )
    found: list[Decimal] = []
    seen: set = set()
    for submission, signal in rows:
        if (submission.canonical_order_type or "") not in ENTRY_TYPES or signal.id in seen:
            continue
        seen.add(signal.id)
        filled = _money(submission.average_fill_price)
        planned = _money(signal.entry_price)
        if filled <= 0 or planned <= 0:
            continue
        # Positive is worse for us: a long that paid more, a short that received
        # less. Reported in rupees a share, because that is how a trader reads a
        # fill and how the cap is expressed.
        drift = (filled - planned) if signal.side.upper() == "LONG" else (planned - filled)
        found.append(drift.quantize(Decimal("0.01")))
    return found


def summarise_slippage(entry_drifts: list[Decimal], records: list[TradeRecord]) -> Slippage:
    trade_values = [record.slippage for record in records if record.slippage is not None]
    return Slippage(
        entry_trades=len(entry_drifts),
        entry_total=sum(entry_drifts, start=Decimal("0")),
        entry_average=_mean(entry_drifts),
        entry_worst=max(entry_drifts) if entry_drifts else None,
        trade_trades=len(trade_values),
        trade_total=sum(trade_values, start=Decimal("0")),
        trade_average=_mean(trade_values),
        trade_worst=min(trade_values) if trade_values else None,
    )


def summarise_expectancy(records: list[TradeRecord]) -> Expectancy:
    """Per-trade economics from closed trades only.

    An open trade has no result. Including its unrealised figure would report a
    position as a finding, and a position that is still moving is the one thing
    a measurement must not be built on.
    """
    closed = [record for record in records if not record.is_open]
    result = Expectancy(trades=len(closed))
    if not closed:
        return result

    wins = [record.net_pnl for record in closed if record.net_pnl > Decimal("1")]
    losses = [record.net_pnl for record in closed if record.net_pnl < Decimal("-1")]
    result.wins, result.losses = len(wins), len(losses)
    result.scratches = len(closed) - len(wins) - len(losses)
    decided = len(wins) + len(losses)
    if decided:
        result.win_rate_percent = (Decimal(len(wins)) * 100 / Decimal(decided)).quantize(Decimal("0.1"))
    result.average_win = _mean(wins)
    result.average_loss = _mean(losses)
    result.gross_per_trade = _mean([record.gross_pnl for record in closed])
    result.net_per_trade = _mean([record.net_pnl for record in closed])
    multiples = [record.r_multiple for record in closed if record.r_multiple is not None]
    result.average_r = _mean(multiples)

    # The win rate this profile needs to come out level: average loss over the
    # sum of the two averages. Undefined when one side has never happened, and
    # left absent rather than guessed at.
    if result.average_win is not None and result.average_loss is not None:
        span = result.average_win + abs(result.average_loss)
        if span > 0:
            result.break_even_win_rate_percent = (abs(result.average_loss) * 100 / span).quantize(Decimal("0.1"))
    return result


def summarise_charges(records: list[TradeRecord], days: list[DaySummary]) -> Charges:
    closed = [record for record in records if not record.is_open]
    charges = Charges(
        gross=sum((record.gross_pnl for record in closed), start=Decimal("0")),
        estimated=sum((record.charges for record in closed), start=Decimal("0")),
    )
    settled = [day for day in days if day.broker_charges is not None]
    charges.broker_days = len(settled)
    charges.days_pending = sum(1 for day in days if day.live_trades and day.broker_charges is None)
    if settled:
        charges.broker = sum((day.broker_charges or Decimal("0") for day in settled), start=Decimal("0"))
    cost = charges.broker if charges.broker is not None else charges.estimated
    if closed:
        charges.per_trade = (cost / len(closed)).quantize(Decimal("0.01"))
    if charges.gross > 0:
        charges.percent_of_gross = (cost * 100 / charges.gross).quantize(Decimal("0.1"))
    return charges


def summarise_strategies(records: list[TradeRecord]) -> list[StrategyQuality]:
    grouped: dict[str, list[TradeRecord]] = {}
    for record in records:
        if not record.is_open:
            grouped.setdefault(record.strategy_version, []).append(record)
    rows = [
        StrategyQuality(
            strategy_version=version,
            trades=len(items),
            wins=sum(1 for item in items if item.net_pnl > Decimal("1")),
            net=sum((item.net_pnl for item in items), start=Decimal("0")),
            net_per_trade=_mean([item.net_pnl for item in items]),
            average_r=_mean([item.r_multiple for item in items if item.r_multiple is not None]),
        )
        for version, items in grouped.items()
    ]
    return sorted(rows, key=lambda row: row.net, reverse=True)


def read_verdict(expectancy: Expectancy) -> str:
    """What this sample can support, in words, before anyone reads a number.

    Deliberately blunt. A per-trade expectancy computed from nine trades looks
    exactly like one computed from nine hundred, and the only thing standing
    between the two is this sentence.
    """
    trades = expectancy.trades
    if trades == 0:
        return "No closed live trades in this range. There is nothing here to measure yet."
    if trades < TOO_FEW:
        return (
            f"{trades} closed live trade{'s' if trades != 1 else ''}. This is not a sample. Nothing below is "
            f"evidence of an edge — it is evidence about execution, which is what it is useful for. "
            f"Judging a strategy needs {TOO_FEW}+ trades, and trusting the judgement needs {INDICATIVE}+."
        )
    if trades < INDICATIVE:
        return (
            f"{trades} closed live trades. Enough to see execution clearly and to form a provisional view of "
            f"the strategy, not enough to act on it. {INDICATIVE}+ before a figure here should change position "
            "size or retire a strategy."
        )
    return (
        f"{trades} closed live trades. A sample worth acting on, provided the period covers more than one kind "
        "of market."
    )


def build_notes(funnel: Funnel, slippage: Slippage, charges: Charges, expectancy: Expectancy) -> list[str]:
    """The handful of readings worth saying out loud, and only when true."""
    notes: list[str] = []
    rate = funnel.fill_rate_percent
    if rate is not None and funnel.sent >= 5 and rate < 70:
        notes.append(
            f"Only {rate}% of live entries filled. A capped limit refuses a bad price by design, but a fill "
            "rate this low means the strategy is being measured on the trades it was slowest to get into. "
            "Widening the entry cap buys fills at the cost of a smaller position."
        )
    if funnel.unknown:
        notes.append(
            f"{funnel.unknown} live entr{'ies have' if funnel.unknown != 1 else 'y has'} no fill record, so the "
            "fill rate is computed without them rather than guessing."
        )
    if charges.percent_of_gross is not None and charges.percent_of_gross >= 50:
        source = "the broker's own figures" if charges.broker is not None else "this system's estimate"
        notes.append(
            f"Charges came to {charges.percent_of_gross}% of gross profit, from {source}. At this ratio the "
            "cost of trading, not the strategy, is deciding the result — fewer and larger trades move this "
            "number more than any entry rule will."
        )
    if charges.days_pending:
        notes.append(
            f"{charges.days_pending} live day(s) are still waiting on the broker's settled charges, so the cost "
            "figures lean on this system's estimate for those."
        )
    if (
        expectancy.break_even_win_rate_percent is not None
        and expectancy.win_rate_percent is not None
        and expectancy.trades >= TOO_FEW
    ):
        gap = expectancy.win_rate_percent - expectancy.break_even_win_rate_percent
        if gap < 0:
            notes.append(
                f"The win rate is {abs(gap)} points below what this profile needs to break even. Either the "
                "winners are too small for the losers, or the costs are too large for both."
            )
    if slippage.entry_average is not None and slippage.entry_average > 0 and slippage.entry_trades >= 5:
        notes.append(
            f"Entries filled {slippage.entry_average} a share worse than planned on average. That is what the "
            "entry cap is sized against; if it is close to the cap, the cap is doing its job and the plan's "
            "entry price is optimistic."
        )
    return notes


async def build_report(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    records: list[TradeRecord],
    days: list[DaySummary],
) -> QualityReport:
    """The whole report, from the live trades of one range.

    ``records`` must already be narrowed to live trades: a paper row has no
    broker fill behind it, so including one would dilute every figure here with
    a trade that cost nothing and filled perfectly.
    """
    funnel = await read_funnel(session, from_date, to_date)
    entry_drifts = await read_entry_slippage(session, from_date, to_date)
    slippage = summarise_slippage(entry_drifts, records)
    expectancy = summarise_expectancy(records)
    charges = summarise_charges(records, days)
    return QualityReport(
        from_date=from_date,
        to_date=to_date,
        funnel=funnel,
        slippage=slippage,
        expectancy=expectancy,
        charges=charges,
        strategies=summarise_strategies(records),
        verdict=read_verdict(expectancy),
        notes=build_notes(funnel, slippage, charges, expectancy),
    )
