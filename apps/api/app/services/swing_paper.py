"""The swing desk on paper: one evening at a time, with the backtest's own rules.

Phase 3. Each trading day, after its daily candles are in:

1. Positions already held take that day's candle: a pending exit at the open,
   the stop, the 2R target, then the trail and the holding limit for tomorrow.
2. The previous evening's plans are filled, skipped on a gap, or lapse.
3. Positions with results due within ``RESULTS_EXIT_DAYS`` are marked to be
   sold at the next open.
4. If NIFTY is above its average, new setups become tomorrow's plans, inside
   the desk's limits, avoiding shares with results due soon.

Steps 1, 2 and 4 are ``swing_backtest``'s own functions, called with the same
rules. A paper desk that filled and exited differently from its backtest would
make the backtest say nothing about it.

**Paper only.** Nothing here can reach a broker: no order request is built, the
live risk engine refuses every swing order regardless, and the only tables
written are ``swing_paper_positions`` and this desk's progress marker. The
intraday desk's tables, ceilings and square-off are not touched.

**Each day once.** Progress is recorded per day processed; a day already done is
never redone, so a restarted worker cannot advance a position twice. The first
run starts from the latest day with data, not from the start of the history.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting, CorporateEvent, SwingPaperPosition
from app.services.market_calculations import CompletedCandle
from app.services.swing_backtest import (
    CostModel,
    SwingRules,
    SwingTrade,
    advance_open,
    choose_plans,
    fill_plan,
    find_setups,
    regime_by_day,
)
from app.services.swing_strategies import STRATEGIES, Series, SwingSetup

PROGRESS_KEY = "swing_paper_progress"
RESULTS_AVOID_DAYS = 5  # no new plan when results are due within this many calendar days
RESULTS_EXIT_DAYS = 3  # sell at the next open when results are due within this many

PLANNED, OPEN, CLOSED, CANCELLED = "PLANNED", "OPEN", "CLOSED", "CANCELLED"


# --- rows <-> the backtest's trade --------------------------------------------------


def setup_of(row: SwingPaperPosition) -> SwingSetup:
    saved = (row.snapshot or {}).get("setup", {})
    return SwingSetup(
        strategy=row.strategy_version,
        signal_date=row.plan_date,
        entry_trigger=Decimal(str(row.entry_trigger)),
        initial_stop=Decimal(str(row.initial_stop)),
        target=Decimal(str(row.target_price)),
        rank=float(saved.get("rank", 0.0)),
        max_holding_days=int(row.max_holding_days),
        trail=saved.get("trail", "ema20"),
        reasons=saved.get("reasons", {}),
    )


def trade_of(row: SwingPaperPosition) -> SwingTrade:
    state = (row.snapshot or {}).get("state", {})
    trade = SwingTrade(
        token=row.instrument_token,
        sector=row.sector or "",
        setup=setup_of(row),
        quantity=int(row.quantity),
        entry_date=row.entry_date,
        entry_price=Decimal(str(row.entry_price)),
        stop=Decimal(str(row.stop_price)),
    )
    trade.remaining = int(state.get("remaining", row.quantity))
    trade.proceeds = Decimal(str(state.get("proceeds", "0")))
    trade.sales = int(state.get("sales", 0))
    trade.partial_done = bool(state.get("partial_done", False))
    trade.partial_price = Decimal(state["partial_price"]) if state.get("partial_price") else None
    trade.holding_days = int(state.get("holding_days", 0))
    trade.exit_next_open = state.get("exit_next_open")
    trade.charges = Decimal(str(state.get("charges", "0")))
    return trade


def save_trade(row: SwingPaperPosition, trade: SwingTrade, day: date) -> None:
    """Write the trade's state back onto its row, closed or not."""
    row.entry_price = trade.entry_price
    row.entry_date = trade.entry_date
    row.stop_price = trade.stop
    if trade.partial_done and not row.partial_exit_quantity:
        half = trade.quantity // 2
        row.partial_exit_quantity = half
        row.partial_exit_price = trade.partial_price if half else None
    snapshot = dict(row.snapshot or {})
    snapshot["state"] = {
        "remaining": trade.remaining,
        "proceeds": str(trade.proceeds),
        "sales": trade.sales,
        "partial_done": trade.partial_done,
        "partial_price": str(trade.partial_price) if trade.partial_price is not None else None,
        "holding_days": trade.holding_days,
        "exit_next_open": trade.exit_next_open,
        "charges": str(trade.charges),
        "last_day": day.isoformat(),
    }
    row.snapshot = snapshot
    if trade.exit_date:
        row.status = CLOSED
        row.exit_date = trade.exit_date
        row.exit_reason = trade.exit_reason
        last_sale = trade.quantity - (row.partial_exit_quantity or 0)
        partial_value = (row.partial_exit_price or Decimal("0")) * (row.partial_exit_quantity or 0)
        row.exit_price = (trade.proceeds - partial_value) / last_sale if last_sale else None
        row.gross_pnl = trade.gross_pnl.quantize(Decimal("0.01"))
        row.charges = trade.charges.quantize(Decimal("0.01"))
        row.net_pnl = trade.net_pnl.quantize(Decimal("0.01"))
    else:
        row.status = OPEN


def plan_row(token: str, symbol: str, sector: str, setup: SwingSetup, quantity: int) -> SwingPaperPosition:
    return SwingPaperPosition(
        instrument_token=token,
        symbol=symbol,
        sector=sector,
        strategy_version=setup.strategy,
        status=PLANNED,
        plan_date=setup.signal_date,
        entry_trigger=setup.entry_trigger,
        quantity=quantity,
        initial_stop=setup.initial_stop,
        stop_price=setup.initial_stop,
        target_price=setup.target,
        risk_amount=(setup.risk_per_share * quantity).quantize(Decimal("0.01")),
        max_holding_days=setup.max_holding_days,
        snapshot={"setup": {"rank": setup.rank, "trail": setup.trail, "reasons": setup.reasons}},
    )


# --- one evening ------------------------------------------------------------------


@dataclass
class EveningReport:
    day: date
    regime_ok: bool = False
    filled: list[SwingPaperPosition] = field(default_factory=list)
    lapsed: list[tuple[SwingPaperPosition, str]] = field(default_factory=list)
    closed: list[SwingPaperPosition] = field(default_factory=list)
    planned: list[SwingPaperPosition] = field(default_factory=list)
    refused: list[tuple[str, str, str]] = field(default_factory=list)  # symbol, strategy, reason
    results_exits: list[str] = field(default_factory=list)
    missing_candles: list[str] = field(default_factory=list)


def results_due(events: Sequence[CorporateEvent], day: date, within_days: int) -> dict[str, str]:
    """Shares with results announced within ``within_days`` after ``day``."""
    due: dict[str, str] = {}
    for event in events:
        if event.event_type == "RESULTS" and day <= event.ex_date <= day + timedelta(days=within_days):
            due[event.instrument_token] = f"results on {event.ex_date.isoformat()}"
    return due


async def run_evening(
    session: AsyncSession,
    day: date,
    *,
    universe: dict[str, Sequence[CompletedCandle]],
    benchmark: Sequence[CompletedCandle],
    sectors: dict[str, str],
    symbols: dict[str, str],
    events: Sequence[CorporateEvent] = (),
    rules: SwingRules | None = None,
    costs: CostModel | None = None,
    strategies: dict | None = None,
) -> EveningReport:
    """Process one trading day. The caller commits."""
    rules = rules or SwingRules()
    costs = costs or CostModel()
    chosen = {name: fn for name, fn in (strategies or STRATEGIES).items() if name in rules.strategies}
    report = EveningReport(day=day)

    series = {token: Series.of(bars) for token, bars in universe.items() if bars}
    position_of = {token: {bar.session_date: i for i, bar in enumerate(s.bars)} for token, s in series.items()}

    # 1. Positions already held.
    open_rows = list((await session.scalars(select(SwingPaperPosition).where(SwingPaperPosition.status == OPEN))).all())
    for row in open_rows:
        if (row.snapshot or {}).get("state", {}).get("last_day") == day.isoformat():
            continue
        i = position_of.get(row.instrument_token, {}).get(day)
        if i is None:
            report.missing_candles.append(row.symbol)
            continue
        trade = trade_of(row)
        advance_open(trade, series[row.instrument_token], i, costs)
        save_trade(row, trade, day)
        if row.status == CLOSED:
            report.closed.append(row)

    # 2. The previous evening's plans.
    planned = (
        await session.scalars(
            select(SwingPaperPosition).where(SwingPaperPosition.status == PLANNED, SwingPaperPosition.plan_date < day)
        )
    ).all()
    for row in planned:
        i = position_of.get(row.instrument_token, {}).get(day)
        if i is None:
            row.status, row.exit_reason = CANCELLED, "no candle"
            report.lapsed.append((row, "no candle"))
            continue
        trade, reason = fill_plan(
            row.instrument_token,
            row.sector or "",
            setup_of(row),
            int(row.quantity),
            series[row.instrument_token].bars[i],
            rules,
            costs,
        )
        if trade is None:
            row.status, row.exit_reason = CANCELLED, reason
            report.lapsed.append((row, reason))
            continue
        save_trade(row, trade, day)
        report.filled.append(row)
        if row.status == CLOSED:
            report.closed.append(row)

    # 3. Results close ahead: sell at the next open.
    still_open = [row for row in open_rows + list(planned) if row.status == OPEN]
    exit_soon = results_due(events, day, RESULTS_EXIT_DAYS)
    for row in still_open:
        if row.instrument_token in exit_soon:
            snapshot = dict(row.snapshot or {})
            state = dict(snapshot.get("state", {}))
            if not state.get("exit_next_open"):
                state["exit_next_open"] = "results"
                snapshot["state"] = state
                row.snapshot = snapshot
                report.results_exits.append(row.symbol)

    # 4. Tomorrow's plans.
    already = await session.scalar(select(SwingPaperPosition.id).where(SwingPaperPosition.plan_date == day).limit(1))
    bench_close = {bar.session_date: float(bar.close) for bar in benchmark}
    report.regime_ok = bool(regime_by_day(benchmark, rules.regime_sma).get(day))
    if report.regime_ok and already is None:
        plans, refused = choose_plans(
            find_setups(series, position_of, bench_close, day, chosen),
            [trade_of(row) for row in still_open],
            sectors,
            rules,
            avoid=results_due(events, day, RESULTS_AVOID_DAYS),
        )
        for token, setup, quantity in plans:
            row = plan_row(token, symbols.get(token, token), sectors.get(token, ""), setup, quantity)
            session.add(row)
            report.planned.append(row)
        report.refused = [(symbols.get(token, token), setup.strategy, reason) for token, setup, reason in refused]
    return report


# --- progress -----------------------------------------------------------------------


async def last_processed(session: AsyncSession) -> date | None:
    row = await session.get(ApplicationSetting, PROGRESS_KEY)
    value = (row.value or {}).get("last_day") if row else None
    return date.fromisoformat(value) if value else None


async def mark_processed(session: AsyncSession, day: date) -> None:
    row = await session.get(ApplicationSetting, PROGRESS_KEY)
    if row is None:
        session.add(ApplicationSetting(key=PROGRESS_KEY, value={"last_day": day.isoformat()}))
    else:
        row.value = {"last_day": day.isoformat()}


def days_to_process(available: Sequence[date], last: date | None) -> list[date]:
    """Trading days with data not yet processed, oldest first. A first run takes only the latest."""
    days = sorted(set(available))
    if not days:
        return []
    if last is None:
        return [days[-1]]
    return [day for day in days if day > last]


def summary_message(report: EveningReport) -> str:
    """The evening's Telegram message: what changed, and tomorrow's plan."""
    lines = [f"📒 <b>SWING DESK — PAPER ONLY</b> ({report.day:%d %b %Y})", "No real order is placed by this desk."]
    for row in report.filled:
        lines.append(f"✅ Bought {row.symbol} {row.quantity} @ {row.entry_price} (stop {row.stop_price})")
    for row in report.closed:
        lines.append(f"🔚 Closed {row.symbol}: {row.exit_reason}, net ₹{row.net_pnl}")
    for row, reason in report.lapsed:
        lines.append(f"⏭ Not bought {row.symbol}: {reason}")
    for symbol in report.results_exits:
        lines.append(f"📅 {symbol}: results soon, selling at the next open")
    if not report.regime_ok:
        lines.append("NIFTY is below its 50-day average: no new plans tonight.")
    elif report.planned:
        lines.append("<b>Plan for the next session</b>")
        for row in report.planned:
            lines.append(
                f"• {row.symbol}: buy-stop {row.entry_trigger}, stop {row.initial_stop}, qty {row.quantity},"
                f" risk ₹{row.risk_amount} ({row.strategy_version})"
            )
    else:
        lines.append("No new setup fits tonight.")
    if report.missing_candles:
        lines.append("⚠ No candle today for: " + ", ".join(report.missing_candles))
    return "\n".join(lines)
