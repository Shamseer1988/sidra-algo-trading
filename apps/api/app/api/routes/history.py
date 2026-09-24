"""The trading record, the way a broker statement reads it.

Four surfaces over one service: a range total, a day-by-day list, a trade list,
and one trade in full. Plus exports, because a record that cannot leave the
application is not much of a record — an accountant does not log in.

Every response carries gross, charges and net separately, and a reconciliation
status per day and per trade. Nothing here ever writes.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.services import trade_history
from app.services.trading_calendar import MARKET_TIMEZONE

router = APIRouter(prefix="/history", tags=["History"])

# A month, which is the window an operator actually reviews. Long enough to hold
# a losing streak in view, short enough that the page is not a download.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 400


def _today() -> date:
    return datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()


def _range(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    end = to_date or _today()
    begin = from_date or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if begin > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The start of the range is after its end.")
    if (end - begin).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That range covers {(end - begin).days} days; {MAX_WINDOW_DAYS} is the most this screen will load "
            "at once. Narrow it, or use the export.",
        )
    return begin, end


class TradeResponse(BaseModel):
    position_id: UUID
    signal_id: UUID
    session_date: str
    instrument_token: str
    script_name: str
    side: str
    strategy_version: str
    status: str
    execution_mode: str
    is_open: bool
    quantity: int
    open_quantity: int
    entry_price: Decimal | None
    exit_price: Decimal | None
    stop_price: Decimal
    target_price: Decimal
    opened_at: str | None
    closed_at: str | None
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    unrealized_pnl: Decimal
    risk_amount: Decimal
    r_multiple: Decimal | None
    reconciliation: str
    reconciliation_label: str
    reconciliation_note: str


class DayResponse(BaseModel):
    session_date: str
    trades: int
    open_trades: int
    wins: int
    losses: int
    scratches: int
    win_rate_percent: Decimal | None
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    unrealized_pnl: Decimal
    best_trade: Decimal | None
    worst_trade: Decimal | None
    live_trades: int
    halt_reason: str | None
    reconciliation: str
    reconciliation_label: str
    reconciliation_note: str
    broker: str | None
    broker_realized_pnl: Decimal | None
    broker_charges: Decimal | None
    broker_fetched_at: str | None


class OverviewResponse(BaseModel):
    from_date: str
    to_date: str
    trading_days: int
    trades: int
    open_trades: int
    wins: int
    losses: int
    scratches: int
    win_rate_percent: Decimal | None
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    best_day: Decimal | None
    worst_day: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    charges_as_percent_of_gross: Decimal | None
    halted_days: int
    live_trades: int
    reconciliation_counts: dict[str, int]
    reconciliation_labels: dict[str, str]


class OrderResponse(BaseModel):
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
    created_at: str


class FillResponse(BaseModel):
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
    occurred_at: str


class TradeDetailResponse(BaseModel):
    trade: TradeResponse
    orders: list[OrderResponse]
    fills: list[FillResponse]


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _trade(record: trade_history.TradeRecord) -> TradeResponse:
    return TradeResponse(
        position_id=record.position_id,
        signal_id=record.signal_id,
        session_date=record.session_date.isoformat(),
        instrument_token=record.instrument_token,
        script_name=record.script_name,
        side=record.side,
        strategy_version=record.strategy_version,
        status=record.status,
        execution_mode=record.execution_mode,
        is_open=record.is_open,
        quantity=record.quantity,
        open_quantity=record.open_quantity,
        entry_price=record.entry_price,
        exit_price=record.exit_price,
        stop_price=record.stop_price,
        target_price=record.target_price,
        opened_at=_stamp(record.opened_at),
        closed_at=_stamp(record.closed_at),
        gross_pnl=record.gross_pnl,
        charges=record.charges,
        net_pnl=record.net_pnl,
        unrealized_pnl=record.unrealized_pnl,
        risk_amount=record.risk_amount,
        r_multiple=record.r_multiple,
        reconciliation=record.reconciliation,
        reconciliation_label=trade_history.STATUS_LABELS[record.reconciliation],
        reconciliation_note=record.reconciliation_note,
    )


def _day(summary: trade_history.DaySummary) -> DayResponse:
    return DayResponse(
        session_date=summary.session_date.isoformat(),
        trades=summary.trades,
        open_trades=summary.open_trades,
        wins=summary.wins,
        losses=summary.losses,
        scratches=summary.scratches,
        win_rate_percent=summary.win_rate_percent,
        gross_pnl=summary.gross_pnl,
        charges=summary.charges,
        net_pnl=summary.net_pnl,
        unrealized_pnl=summary.unrealized_pnl,
        best_trade=summary.best_trade,
        worst_trade=summary.worst_trade,
        live_trades=summary.live_trades,
        halt_reason=summary.halt_reason,
        reconciliation=summary.reconciliation,
        reconciliation_label=trade_history.STATUS_LABELS[summary.reconciliation],
        reconciliation_note=summary.reconciliation_note,
        broker=summary.broker,
        broker_realized_pnl=summary.broker_realized_pnl,
        broker_charges=summary.broker_charges,
        broker_fetched_at=_stamp(summary.broker_fetched_at),
    )


@router.get("/overview", response_model=OverviewResponse)
async def overview(
    session: DbSession,
    _: CurrentUser,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> OverviewResponse:
    begin, end = _range(from_date, to_date)
    records = await trade_history.load_trades(session, begin, end)
    days = await trade_history.summarise_days(session, records, begin, end)
    totals = trade_history.summarise_range(records, days, begin, end)
    return OverviewResponse(
        from_date=totals.from_date.isoformat(),
        to_date=totals.to_date.isoformat(),
        trading_days=totals.trading_days,
        trades=totals.trades,
        open_trades=totals.open_trades,
        wins=totals.wins,
        losses=totals.losses,
        scratches=totals.scratches,
        win_rate_percent=totals.win_rate_percent,
        gross_pnl=totals.gross_pnl,
        charges=totals.charges,
        net_pnl=totals.net_pnl,
        best_day=totals.best_day,
        worst_day=totals.worst_day,
        largest_win=totals.largest_win,
        largest_loss=totals.largest_loss,
        average_win=totals.average_win,
        average_loss=totals.average_loss,
        profit_factor=totals.profit_factor,
        expectancy=totals.expectancy,
        charges_as_percent_of_gross=totals.charges_as_percent_of_gross,
        halted_days=totals.halted_days,
        live_trades=totals.live_trades,
        reconciliation_counts=totals.reconciliation_counts,
        reconciliation_labels=trade_history.STATUS_LABELS,
    )


@router.get("/daily", response_model=list[DayResponse])
async def daily(
    session: DbSession,
    _: CurrentUser,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> list[DayResponse]:
    begin, end = _range(from_date, to_date)
    records = await trade_history.load_trades(session, begin, end)
    return [_day(summary) for summary in await trade_history.summarise_days(session, records, begin, end)]


@router.get("/trades", response_model=list[TradeResponse])
async def trades(
    session: DbSession,
    _: CurrentUser,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    session_date: date | None = Query(default=None),
    instrument_token: str | None = Query(default=None),
    strategy_version: str | None = Query(default=None),
) -> list[TradeResponse]:
    begin, end = (session_date, session_date) if session_date else _range(from_date, to_date)
    records = await trade_history.load_trades(
        session, begin, end, instrument_token=instrument_token, strategy_version=strategy_version
    )
    return [_trade(record) for record in records]


@router.get("/trades/{position_id}", response_model=TradeDetailResponse)
async def trade_detail(session: DbSession, _: CurrentUser, position_id: UUID) -> TradeDetailResponse:
    found = await trade_history.load_trade_detail(session, position_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade.")
    record, orders, fills = found
    return TradeDetailResponse(
        trade=_trade(record),
        orders=[
            OrderResponse(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                order_role=order.order_role,
                order_type=order.order_type,
                side=order.side,
                status=order.status,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                average_fill_price=order.average_fill_price,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                fee_total=order.fee_total,
                rejection_reason=order.rejection_reason,
                created_at=order.created_at.isoformat(),
            )
            for order in orders
        ],
        fills=[
            FillResponse(
                fill_id=fill.fill_id,
                order_id=fill.order_id,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                gross_value=fill.gross_value,
                slippage_amount=fill.slippage_amount,
                brokerage=fill.brokerage,
                stt=fill.stt,
                exchange_charge=fill.exchange_charge,
                gst=fill.gst,
                sebi_charge=fill.sebi_charge,
                stamp_duty=fill.stamp_duty,
                total_fees=fill.total_fees,
                occurred_at=fill.occurred_at.isoformat(),
            )
            for fill in fills
        ],
    )


# --- exports --------------------------------------------------------------

# The trade sheet's columns, in the order an accountant reads them. Gross,
# charges and net are three columns and never one: a spreadsheet that carried
# only "P&L" would be re-derived wrongly by whoever opened it next.
TRADE_COLUMNS = (
    ("session_date", "Session date"),
    ("script_name", "Instrument"),
    ("side", "Side"),
    ("strategy_version", "Strategy"),
    ("execution_mode", "Mode"),
    ("quantity", "Quantity"),
    ("entry_price", "Entry"),
    ("exit_price", "Exit"),
    ("stop_price", "Stop"),
    ("target_price", "Target"),
    ("gross_pnl", "Gross P&L"),
    ("charges", "Charges"),
    ("net_pnl", "Net P&L"),
    ("r_multiple", "R multiple"),
    ("risk_amount", "Risk planned"),
    ("status", "Position status"),
    ("opened_at", "Opened"),
    ("closed_at", "Closed"),
    ("reconciliation_label", "Reconciliation"),
    ("reconciliation_note", "Reconciliation note"),
    ("position_id", "Position id"),
)

DAY_COLUMNS = (
    ("session_date", "Session date"),
    ("trades", "Trades"),
    ("wins", "Wins"),
    ("losses", "Losses"),
    ("scratches", "Scratches"),
    ("win_rate_percent", "Win rate %"),
    ("gross_pnl", "Gross P&L"),
    ("charges", "Charges"),
    ("net_pnl", "Net P&L"),
    ("best_trade", "Best trade"),
    ("worst_trade", "Worst trade"),
    ("live_trades", "Live trades"),
    ("halt_reason", "Day ended by"),
    ("reconciliation_label", "Reconciliation"),
    ("broker", "Broker"),
    ("broker_realized_pnl", "Broker realised"),
    ("broker_charges", "Broker charges"),
    ("broker_fetched_at", "Broker figures fetched"),
    ("reconciliation_note", "Reconciliation note"),
)


def _cell(value):
    """A value a spreadsheet will not mangle.

    Decimals become floats because openpyxl writes a Decimal as a string, and a
    column of text right-aligned to look like money is the kind of thing nobody
    notices until they sum it.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _rows(items, columns) -> list[list]:
    return [[_cell(getattr(item, key)) for key, _ in columns] for item in items]


async def _sheets(session, begin: date, end: date):
    records = await trade_history.load_trades(session, begin, end)
    days = await trade_history.summarise_days(session, records, begin, end)
    totals = trade_history.summarise_range(records, days, begin, end)
    return [_trade(record) for record in records], [_day(summary) for summary in days], totals


@router.get("/export.csv")
async def export_csv(
    session: DbSession,
    _: CurrentUser,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> StreamingResponse:
    """The trade list as CSV. One sheet's worth, because CSV holds one table."""
    begin, end = _range(from_date, to_date)
    trades_out, _days, _totals = await _sheets(session, begin, end)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in TRADE_COLUMNS])
    writer.writerows(_rows(trades_out, TRADE_COLUMNS))
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="trade-history-{begin}-to-{end}.csv"'},
    )


@router.get("/export.xlsx")
async def export_xlsx(
    session: DbSession,
    _: CurrentUser,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> StreamingResponse:
    """Three sheets: the summary, the days, the trades.

    The summary sheet leads with a line saying charges are estimated locally,
    because a spreadsheet outlives the screen it was exported from and will be
    read by somebody who never saw the reconciliation column.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    begin, end = _range(from_date, to_date)
    trades_out, days_out, totals = await _sheets(session, begin, end)

    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["Trading history", f"{begin} to {end}"])
    summary.append([])
    summary.append(
        [
            "Charges shown are this system's estimate from the published rate card, except where a broker "
            "figure is recorded against the day. Brokers report charges aggregated over a date range, never "
            "per trade."
        ]
    )
    summary.append([])
    for label, value in (
        ("Trading days", totals.trading_days),
        ("Trades", totals.trades),
        ("Open at export", totals.open_trades),
        ("Wins", totals.wins),
        ("Losses", totals.losses),
        ("Scratches", totals.scratches),
        ("Win rate %", totals.win_rate_percent),
        ("Gross P&L", totals.gross_pnl),
        ("Charges", totals.charges),
        ("Net P&L", totals.net_pnl),
        ("Charges as % of gross", totals.charges_as_percent_of_gross),
        ("Best day", totals.best_day),
        ("Worst day", totals.worst_day),
        ("Largest win", totals.largest_win),
        ("Largest loss", totals.largest_loss),
        ("Average win", totals.average_win),
        ("Average loss", totals.average_loss),
        ("Profit factor", totals.profit_factor),
        ("Expectancy a trade", totals.expectancy),
        ("Days ended by a limit", totals.halted_days),
        ("Live trades", totals.live_trades),
    ):
        summary.append([label, _cell(value)])
    summary["A1"].font = Font(bold=True)

    for title, items, columns in (("Days", days_out, DAY_COLUMNS), ("Trades", trades_out, TRADE_COLUMNS)):
        sheet = book.create_sheet(title)
        sheet.append([label for _, label in columns])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for row in _rows(items, columns):
            sheet.append(row)

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="trade-history-{begin}-to-{end}.xlsx"'},
    )
