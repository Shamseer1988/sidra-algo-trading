"""The trading record: what was taken, what it cost, and whether the broker agrees.

Three things this module refuses to do, each because the alternative is a way of
being quietly wrong about money.

**Gross, charges and net are always carried separately.** A single "P&L" figure
is the most common way a trading record lies. A day that made ₹900 before costs
and ₹340 after is not a ₹900 day, and a strategy judged on gross is being judged
on a number nobody can withdraw. Every row here carries all three.

**Charges are labelled as estimates, because they are.** Upstox reports charges
aggregated over a date range and never per trade; Firstock does not report them
per trade either. Per-trade costs in this system are therefore computed locally
from the published rate card and always will be. ``ESTIMATED_CHARGES`` is the
normal steady state for a reconciled day, not a fault — which is why it is a
distinct status rather than a flavour of MISMATCH.

**Broker figures never overwrite local ones.** They live in
``broker_day_snapshots`` and are compared, not merged. The local figure is what
this system believed at the time, which is the thing an audit is actually asking
about; replacing it would leave nothing to audit.

A note on what a "trade" is. One row per ``PaperPosition``, which is one entry
and its exit — not one order and not one fill. That matches how a trader counts
("I took four trades today") and it matches the daily ceiling in
``trade_counter``, which counts an entry's first fill. Counting orders would
double every trade; counting fills would multiply them by however many partials
the simulator produced.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BrokerDaySnapshot,
    LiveOrderSubmission,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperSignal,
    SessionHalt,
)
from app.services.trade_counter import LIVE_PLACED_STATUSES

# Reconciliation vocabulary. Four states, and the distinction between the middle
# two is the one that matters: "we have not asked the broker yet" and "the broker
# does not publish this per trade" are different situations with different fixes.
MATCHED = "MATCHED"
ESTIMATED_CHARGES = "ESTIMATED_CHARGES"
BROKER_DATA_PENDING = "BROKER_DATA_PENDING"
MISMATCH = "MISMATCH"

STATUS_LABELS = {
    MATCHED: "Matched",
    ESTIMATED_CHARGES: "Estimated charges",
    BROKER_DATA_PENDING: "Broker data pending",
    MISMATCH: "Mismatch",
}

# One rupee. Rounding differs between our rate card and the broker's — they round
# per order, per scrip and per segment in an order nobody publishes — so sub-rupee
# disagreement is arithmetic, not a discrepancy. Anything larger is worth a look.
TOLERANCE = Decimal("1.00")

PAPER = "PAPER"
LIVE = "LIVE"

CLOSED_STATUSES = frozenset({"CLOSED", "FLATTENED", "EXITED"})


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


@dataclass(frozen=True)
class TradeRecord:
    """One position, from entry to exit, with its money broken out."""

    position_id: UUID
    signal_id: UUID
    session_date: date
    instrument_token: str
    script_name: str
    side: str
    strategy_version: str
    status: str
    execution_mode: str
    quantity: int
    open_quantity: int
    entry_price: Decimal | None
    exit_price: Decimal | None
    stop_price: Decimal
    target_price: Decimal
    opened_at: datetime | None
    closed_at: datetime | None
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    unrealized_pnl: Decimal
    risk_amount: Decimal
    reconciliation: str
    reconciliation_note: str

    @property
    def is_open(self) -> bool:
        return self.open_quantity > 0 or self.status not in CLOSED_STATUSES

    @property
    def r_multiple(self) -> Decimal | None:
        """Net result as a multiple of the risk that was planned for it.

        Net, not gross. An R measured before costs flatters every strategy by
        exactly the amount it costs to run, which is the amount that decides
        whether it is worth running.
        """
        if self.risk_amount <= 0 or self.is_open:
            return None
        return (self.net_pnl / self.risk_amount).quantize(Decimal("0.01"))


@dataclass
class DaySummary:
    """One session: the day as a trader would read it off a broker statement."""

    session_date: date
    trades: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    gross_pnl: Decimal = Decimal("0")
    charges: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    # Carried separately because the day's net includes it while a position is
    # still open, and a figure that moves with the market should not be read as
    # money that was made.
    unrealized_pnl: Decimal = Decimal("0")
    best_trade: Decimal | None = None
    worst_trade: Decimal | None = None
    live_trades: int = 0
    halt_reason: str | None = None
    reconciliation: str = ESTIMATED_CHARGES
    reconciliation_note: str = ""
    broker_realized_pnl: Decimal | None = None
    broker_charges: Decimal | None = None
    broker_fetched_at: datetime | None = None
    broker: str | None = None

    @property
    def win_rate_percent(self) -> Decimal | None:
        decided = self.wins + self.losses
        if not decided:
            return None
        return (Decimal(self.wins) * 100 / Decimal(decided)).quantize(Decimal("0.01"))


@dataclass
class Overview:
    """A date range, totalled. Every figure here is net unless it says otherwise."""

    from_date: date
    to_date: date
    trading_days: int = 0
    trades: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    gross_pnl: Decimal = Decimal("0")
    charges: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    best_day: Decimal | None = None
    worst_day: Decimal | None = None
    largest_win: Decimal | None = None
    largest_loss: Decimal | None = None
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    profit_factor: Decimal | None = None
    expectancy: Decimal | None = None
    charges_as_percent_of_gross: Decimal | None = None
    halted_days: int = 0
    live_trades: int = 0
    reconciliation_counts: dict[str, int] = field(default_factory=dict)

    @property
    def win_rate_percent(self) -> Decimal | None:
        decided = self.wins + self.losses
        if not decided:
            return None
        return (Decimal(self.wins) * 100 / Decimal(decided)).quantize(Decimal("0.01"))


# --- reconciliation -------------------------------------------------------


def reconcile_day(
    *,
    live_trades: int,
    local_gross: Decimal,
    local_charges: Decimal,
    snapshot: BrokerDaySnapshot | None,
) -> tuple[str, str]:
    """Decide what the broker's figures say about a day, and say it in words.

    The interesting case is the third one. Our charges are an estimate and are
    admitted to be; a broker figure that differs from the estimate is new
    information about the estimate, not evidence that the trades are wrong. So a
    charge difference on its own stays ESTIMATED_CHARGES and carries the delta,
    while a P&L difference is a real MISMATCH — it means the fills, quantities or
    prices are not what we recorded, and that is worth stopping for.
    """
    if live_trades == 0:
        return (
            ESTIMATED_CHARGES,
            "Paper session. Charges are this system's estimate from the published rate card, "
            "and there is no broker side to match them against.",
        )
    if snapshot is None:
        return (
            BROKER_DATA_PENDING,
            f"{live_trades} live trade{'s were' if live_trades != 1 else ' was'} taken. "
            "The broker's own figures for this day have not been fetched yet.",
        )

    broker_pnl = None if snapshot.realized_pnl is None else _money(snapshot.realized_pnl)
    broker_charges = None if snapshot.charges is None else _money(snapshot.charges)
    if broker_pnl is None and broker_charges is None:
        return (
            BROKER_DATA_PENDING,
            f"A snapshot from {snapshot.broker} ({snapshot.source}) exists for this day but reported "
            "neither a realised figure nor charges.",
        )

    notes: list[str] = []
    if broker_pnl is not None:
        delta = broker_pnl - local_gross
        if abs(delta) > TOLERANCE:
            return (
                MISMATCH,
                f"{snapshot.broker} reports ₹{broker_pnl} realised against our ₹{local_gross} "
                f"(off by ₹{delta}). Our figures are unchanged; this needs explaining before the "
                "day is trusted.",
            )
        notes.append(f"realised P&L agrees with {snapshot.broker} within ₹{TOLERANCE}")

    if broker_charges is not None:
        delta = broker_charges - local_charges
        if abs(delta) > TOLERANCE:
            return (
                ESTIMATED_CHARGES,
                f"{snapshot.broker} charged ₹{broker_charges} against our estimate of ₹{local_charges} "
                f"(off by ₹{delta}). The broker's figure is the real cost; the local estimate is kept "
                "as recorded.",
            )
        notes.append(f"charges agree with {snapshot.broker} within ₹{TOLERANCE}")

    if broker_charges is None:
        return (
            ESTIMATED_CHARGES,
            f"{snapshot.broker} reported a realised figure but no charges for this day, so the cost "
            "shown is still this system's estimate. " + ("; ".join(notes).capitalize() + "." if notes else ""),
        )

    return MATCHED, ("; ".join(notes).capitalize() + ".") if notes else "Agrees with the broker."


def trade_reconciliation(record_mode: str, day_status: str, day_note: str) -> tuple[str, str]:
    """A trade's status, which is its day's status only if the trade was live.

    A paper trade on a day that also had live trades is not pending anything and
    cannot mismatch: there is no broker order behind it to compare. Inheriting
    the day's status would mark simulated rows as awaiting broker data that will
    never mention them.
    """
    if record_mode == PAPER:
        return (
            ESTIMATED_CHARGES,
            "Simulated. Charges are this system's estimate from the published rate card.",
        )
    if day_status in {MATCHED, MISMATCH}:
        return day_status, day_note
    return day_status, day_note


# --- loading --------------------------------------------------------------


async def live_signal_ids(session: AsyncSession, from_date: date, to_date: date) -> set[UUID]:
    """Signals that actually reached a broker, so a trade can be called live.

    Keyed off ``LiveOrderSubmission`` rather than a flag on the position, because
    the submission record is the only thing written on the path that talks to a
    broker. A position marked live by anything else would be a claim nobody
    checked.
    """
    rows = await session.execute(
        select(LiveOrderSubmission.paper_signal_id, PaperSignal.session_date)
        .join(PaperSignal, PaperSignal.id == LiveOrderSubmission.paper_signal_id)
        .where(
            LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
            LiveOrderSubmission.paper_signal_id.is_not(None),
            PaperSignal.session_date >= from_date,
            PaperSignal.session_date <= to_date,
        )
    )
    return {signal_id for signal_id, _ in rows.all()}


async def latest_broker_snapshots(
    session: AsyncSession, from_date: date, to_date: date
) -> dict[date, BrokerDaySnapshot]:
    """The most recent snapshot per session date.

    The table is append-only, so a day commonly holds several. The newest is the
    one to compare against; the older ones remain as the record of how the
    broker's own figures settled.
    """
    rows = (
        await session.scalars(
            select(BrokerDaySnapshot)
            .where(BrokerDaySnapshot.session_date >= from_date, BrokerDaySnapshot.session_date <= to_date)
            .order_by(BrokerDaySnapshot.session_date.asc(), BrokerDaySnapshot.fetched_at.asc())
        )
    ).all()
    # Ascending, so the last write per date wins and the newest survives.
    return {row.session_date: row for row in rows}


async def load_trades(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    *,
    instrument_token: str | None = None,
    strategy_version: str | None = None,
) -> list[TradeRecord]:
    """Every position in the range, as trades, newest first."""
    statement = (
        select(PaperPosition, PaperSignal)
        .join(PaperSignal, PaperSignal.id == PaperPosition.paper_signal_id)
        .where(PaperPosition.session_date >= from_date, PaperPosition.session_date <= to_date)
    )
    if instrument_token:
        statement = statement.where(PaperPosition.instrument_token == instrument_token)
    if strategy_version:
        statement = statement.where(PaperPosition.strategy_version == strategy_version)

    rows = (
        await session.execute(
            statement.order_by(PaperPosition.session_date.desc(), PaperPosition.opened_at.desc().nulls_last())
        )
    ).all()

    live_ids = await live_signal_ids(session, from_date, to_date)
    snapshots = await latest_broker_snapshots(session, from_date, to_date)

    # The day's verdict is computed once per date from the whole day, then each
    # trade reads it. Computing it per trade would ask the same question of the
    # same broker snapshot once per row and could answer differently for two
    # trades on the same day.
    per_day: dict[date, list] = {}
    for position, signal in rows:
        per_day.setdefault(position.session_date, []).append((position, signal))

    day_verdicts: dict[date, tuple[str, str]] = {}
    for session_date, entries in per_day.items():
        gross = sum((_money(item.realized_pnl) for item, _ in entries), start=Decimal("0"))
        charges = sum((_money(item.fees_total) for item, _ in entries), start=Decimal("0"))
        live_count = sum(1 for _, sig in entries if sig.id in live_ids)
        day_verdicts[session_date] = reconcile_day(
            live_trades=live_count,
            local_gross=gross,
            local_charges=charges,
            snapshot=snapshots.get(session_date),
        )

    from app.services.trading_symbols import resolve_script_names

    names = await resolve_script_names(session, {position.instrument_token for position, _ in rows})

    records: list[TradeRecord] = []
    for position, signal in rows:
        mode = LIVE if signal.id in live_ids else PAPER
        day_status, day_note = day_verdicts[position.session_date]
        status, note = trade_reconciliation(mode, day_status, day_note)
        records.append(
            TradeRecord(
                position_id=position.id,
                signal_id=signal.id,
                session_date=position.session_date,
                instrument_token=position.instrument_token,
                script_name=names.get(position.instrument_token, position.instrument_token),
                side=position.side,
                strategy_version=position.strategy_version,
                status=position.status,
                execution_mode=mode,
                quantity=position.initial_quantity,
                open_quantity=position.open_quantity,
                entry_price=None if position.average_entry_price is None else _money(position.average_entry_price),
                exit_price=None if position.average_exit_price is None else _money(position.average_exit_price),
                stop_price=_money(position.stop_price),
                target_price=_money(position.target_price),
                opened_at=position.opened_at,
                closed_at=position.closed_at,
                gross_pnl=_money(position.realized_pnl),
                charges=_money(position.fees_total),
                net_pnl=_money(position.total_pnl),
                unrealized_pnl=_money(position.unrealized_pnl),
                risk_amount=_money(signal.risk_amount),
                reconciliation=status,
                reconciliation_note=note,
            )
        )
    return records


# --- aggregation ----------------------------------------------------------

# A trade that netted less than this either way is neither a win nor a loss. An
# exit at the entry price after costs is a scratch, and counting it as a loss
# (net is a few paise negative) would understate every win rate in the system.
SCRATCH = Decimal("1.00")


def _classify(net: Decimal) -> str:
    if net > SCRATCH:
        return "win"
    if net < -SCRATCH:
        return "loss"
    return "scratch"


async def summarise_days(
    session: AsyncSession, records: list[TradeRecord], from_date: date, to_date: date
) -> list[DaySummary]:
    """One row per session date that had activity, newest first."""
    halts = {
        (row.session_date, row.mode): row
        for row in (
            await session.scalars(
                select(SessionHalt).where(SessionHalt.session_date >= from_date, SessionHalt.session_date <= to_date)
            )
        ).all()
    }
    snapshots = await latest_broker_snapshots(session, from_date, to_date)

    days: dict[date, DaySummary] = {}
    for record in records:
        day = days.setdefault(record.session_date, DaySummary(session_date=record.session_date))
        day.trades += 1
        day.gross_pnl += record.gross_pnl
        day.charges += record.charges
        day.net_pnl += record.net_pnl
        day.unrealized_pnl += record.unrealized_pnl
        if record.execution_mode == LIVE:
            day.live_trades += 1
        if record.is_open:
            day.open_trades += 1
            continue
        outcome = _classify(record.net_pnl)
        if outcome == "win":
            day.wins += 1
        elif outcome == "loss":
            day.losses += 1
        else:
            day.scratches += 1
        day.best_trade = record.net_pnl if day.best_trade is None else max(day.best_trade, record.net_pnl)
        day.worst_trade = record.net_pnl if day.worst_trade is None else min(day.worst_trade, record.net_pnl)

    for session_date, day in days.items():
        # A live halt and a paper halt can both exist for one date. The live one
        # is reported first because it is the one that stopped real money.
        halt = halts.get((session_date, LIVE)) or halts.get((session_date, PAPER))
        if halt is not None:
            day.halt_reason = f"{halt.mode}: {halt.reason} at ₹{_money(halt.session_pnl)}"
        snapshot = snapshots.get(session_date)
        day.reconciliation, day.reconciliation_note = reconcile_day(
            live_trades=day.live_trades,
            local_gross=day.gross_pnl,
            local_charges=day.charges,
            snapshot=snapshot,
        )
        if snapshot is not None:
            day.broker = snapshot.broker
            day.broker_fetched_at = snapshot.fetched_at
            day.broker_realized_pnl = None if snapshot.realized_pnl is None else _money(snapshot.realized_pnl)
            day.broker_charges = None if snapshot.charges is None else _money(snapshot.charges)

    return sorted(days.values(), key=lambda item: item.session_date, reverse=True)


def summarise_range(records: list[TradeRecord], days: list[DaySummary], from_date: date, to_date: date) -> Overview:
    """The range, totalled.

    ``profit_factor`` and ``expectancy`` are computed from net figures and are
    left as None rather than as a placeholder when there is nothing to divide by.
    A profit factor of 0 for a range with no losing trades would read as a
    catastrophe; a profit factor that is simply absent reads as what it is.
    """
    overview = Overview(from_date=from_date, to_date=to_date)
    closed = [record for record in records if not record.is_open]
    wins = [record.net_pnl for record in closed if _classify(record.net_pnl) == "win"]
    losses = [record.net_pnl for record in closed if _classify(record.net_pnl) == "loss"]

    overview.trading_days = len(days)
    overview.trades = len(records)
    overview.open_trades = sum(1 for record in records if record.is_open)
    overview.wins = len(wins)
    overview.losses = len(losses)
    overview.scratches = len(closed) - len(wins) - len(losses)
    overview.gross_pnl = sum((record.gross_pnl for record in records), start=Decimal("0"))
    overview.charges = sum((record.charges for record in records), start=Decimal("0"))
    overview.net_pnl = sum((record.net_pnl for record in records), start=Decimal("0"))
    overview.live_trades = sum(1 for record in records if record.execution_mode == LIVE)
    overview.halted_days = sum(1 for day in days if day.halt_reason)

    if days:
        overview.best_day = max(day.net_pnl for day in days)
        overview.worst_day = min(day.net_pnl for day in days)
    if wins:
        overview.largest_win = max(wins)
        overview.average_win = (sum(wins, start=Decimal("0")) / len(wins)).quantize(Decimal("0.01"))
    if losses:
        overview.largest_loss = min(losses)
        overview.average_loss = (sum(losses, start=Decimal("0")) / len(losses)).quantize(Decimal("0.01"))

    won = sum(wins, start=Decimal("0"))
    lost = abs(sum(losses, start=Decimal("0")))
    if lost > 0:
        overview.profit_factor = (won / lost).quantize(Decimal("0.01"))
    if closed:
        overview.expectancy = (sum((record.net_pnl for record in closed), start=Decimal("0")) / len(closed)).quantize(
            Decimal("0.01")
        )
    if overview.gross_pnl > 0:
        # Only meaningful against a gross profit. Against a gross loss the ratio
        # is a number that looks like a percentage and means nothing.
        overview.charges_as_percent_of_gross = (overview.charges * 100 / overview.gross_pnl).quantize(Decimal("0.01"))

    counts: dict[str, int] = {}
    for day in days:
        counts[day.reconciliation] = counts.get(day.reconciliation, 0) + 1
    overview.reconciliation_counts = counts
    return overview


# --- one trade, in full ---------------------------------------------------


@dataclass(frozen=True)
class OrderLine:
    order_id: UUID
    client_order_id: str
    order_role: str
    order_type: str
    side: str
    status: str
    quantity: int
    filled_quantity: int
    average_fill_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    fee_total: Decimal
    rejection_reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class FillLine:
    fill_id: UUID
    order_id: UUID
    side: str
    quantity: int
    price: Decimal
    gross_value: Decimal
    slippage_amount: Decimal
    brokerage: Decimal
    stt: Decimal
    exchange_charge: Decimal
    gst: Decimal
    sebi_charge: Decimal
    stamp_duty: Decimal
    total_fees: Decimal
    occurred_at: datetime


async def load_trade_detail(
    session: AsyncSession, position_id: UUID
) -> tuple[TradeRecord, list[OrderLine], list[FillLine]] | None:
    """One trade with the orders and fills behind it, for the drill-down.

    The fill breakdown is itemised — brokerage, STT, exchange, GST, SEBI, stamp
    duty — rather than rolled into one "charges" figure, because when a local
    estimate disagrees with a broker's bill the only useful next question is
    which line is wrong.
    """
    position = await session.get(PaperPosition, position_id)
    if position is None:
        return None

    records = await load_trades(session, position.session_date, position.session_date)
    record = next((item for item in records if item.position_id == position_id), None)
    if record is None:
        return None

    orders = (
        await session.scalars(
            select(PaperOrder)
            .where(PaperOrder.paper_signal_id == position.paper_signal_id)
            .order_by(PaperOrder.created_at.asc())
        )
    ).all()
    order_ids = [order.id for order in orders]
    fills = (
        (
            await session.scalars(
                select(PaperFill).where(PaperFill.paper_order_id.in_(order_ids)).order_by(PaperFill.occurred_at.asc())
            )
        ).all()
        if order_ids
        else []
    )

    return (
        record,
        [
            OrderLine(
                order_id=order.id,
                client_order_id=order.client_order_id,
                order_role=order.order_role,
                order_type=order.order_type,
                side=order.side,
                status=order.status,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                average_fill_price=None if order.average_fill_price is None else _money(order.average_fill_price),
                limit_price=None if order.limit_price is None else _money(order.limit_price),
                stop_price=None if order.stop_price is None else _money(order.stop_price),
                fee_total=_money(order.fee_total),
                rejection_reason=order.rejection_reason,
                created_at=order.created_at,
            )
            for order in orders
        ],
        [
            FillLine(
                fill_id=fill.id,
                order_id=fill.paper_order_id,
                side=fill.side,
                quantity=fill.quantity,
                price=_money(fill.price),
                gross_value=_money(fill.gross_value),
                slippage_amount=_money(fill.slippage_amount),
                brokerage=_money(fill.brokerage),
                stt=_money(fill.stt),
                exchange_charge=_money(fill.exchange_charge),
                gst=_money(fill.gst),
                sebi_charge=_money(fill.sebi_charge),
                stamp_duty=_money(fill.stamp_duty),
                total_fees=_money(fill.total_fees),
                occurred_at=fill.occurred_at,
            )
            for fill in fills
        ],
    )
