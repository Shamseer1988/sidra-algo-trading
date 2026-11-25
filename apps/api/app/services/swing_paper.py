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
from app.services.swing_strategies import BASE_BREAKOUT, STRATEGIES, TREND_PULLBACK, Series, SwingSetup

PROGRESS_KEY = "swing_paper_progress"

# What the paper desk runs: Trend Pullback only, as a forward test.
#
# Neither strategy passed the Phase 2 gate. On the NAS's two years of NIFTY 200
# history (21 Nov 2024 - 19 Nov 2026), after charges, out-of-sample:
#   Trend Pullback  40 trades  -Rs 33 a trade  (in-sample +Rs 67)
#   Base Breakout    3 trades  -Rs 354 a trade (negative in-sample too)
# Trend Pullback was about +Rs 87 a trade before charges; Base Breakout lost
# either way. So Trend Pullback runs on paper to gather evidence the backtest
# cannot give -- days it has never seen -- and Base Breakout does not run.
# Chosen by Shamseer on 20 Nov 2026. Not validated; paper only.
PAPER_STRATEGIES = (TREND_PULLBACK,)
PAPER_STATUS = "Forward test on paper. Did not pass the backtest gate after charges."

# Which swing strategies run, chosen in the UI (Strategies > control center)
# and stored under this key as {strategy id: "OFF" | "PAPER"}. The default is
# PAPER_STRATEGIES above. "LIVE" is refused: the swing desk has no live limits.
SWING_MODES_KEY = "swing_strategy_modes"
SWING_MODES = ("OFF", "PAPER")

# What the backtest said about each, shown beside the switch so the choice is
# made with the evidence in view. From the NAS run on 20 Nov 2026, after charges.
SWING_EVIDENCE = {
    TREND_PULLBACK: "Backtest 2024-26, out-of-sample: 40 trades, -Rs 33 a trade after charges (in-sample +Rs 67).",
    BASE_BREAKOUT: "Backtest 2024-26, out-of-sample: 3 trades, -Rs 354 a trade; negative in-sample too.",
}
SWING_NAMES = {TREND_PULLBACK: "Trend Pullback", BASE_BREAKOUT: "Base Breakout"}


def default_swing_modes() -> dict[str, str]:
    return {name: ("PAPER" if name in PAPER_STRATEGIES else "OFF") for name in STRATEGIES}


async def swing_modes(session: AsyncSession) -> dict[str, str]:
    """Every swing strategy's mode, the stored choice over the default."""
    row = await session.get(ApplicationSetting, SWING_MODES_KEY)
    stored = row.value if row and isinstance(row.value, dict) else {}
    modes = default_swing_modes()
    modes.update({name: mode for name, mode in stored.items() if name in modes and mode in SWING_MODES})
    return modes


async def paper_strategies(session: AsyncSession) -> tuple[str, ...]:
    """The swing strategies the paper desk runs tonight."""
    return tuple(name for name, mode in (await swing_modes(session)).items() if mode == "PAPER")


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
    new_entries: bool = True,
) -> EveningReport:
    """Process one trading day. The caller commits.

    `new_entries` is False while the swing desk is switched off: positions already
    held are still managed to their exits -- a switch never abandons a position --
    but plans not yet bought are cancelled and no new plan is made.
    """
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
        if not new_entries:
            row.status, row.exit_reason = CANCELLED, "desk switched off"
            report.lapsed.append((row, "desk switched off"))
            continue
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
    if new_entries and report.regime_ok and already is None:
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


# An evening runs once and is never redone, so it waits for the data to be in.
# The intraday scanner stores daily candles for its own shares and the index
# too: on 25 Nov 2026 the index arrived that way, and the evening ran on 34 of
# 201 candles. Not every share, because one can be suspended or newly listed.
COMPLETE_PERCENT = 95
RECENT_SESSIONS = 10


def missing_for_day(
    day: date,
    index_days: Sequence[date],
    traded: dict[str, set[date]],
    needed: Sequence[str] = (),
) -> tuple[int, int, list[str]]:
    """(candles in, candles expected, held shares without one) for ``day``.

    Expected is every share that traded in the sessions just before: several
    sessions, so one badly filled day cannot lower the bar for the next.
    """
    recent = set(sorted(d for d in index_days if d < day)[-RECENT_SESSIONS:])
    expected = {token for token, days in traded.items() if day in days or days & recent}
    present = {token for token in expected if day in traded[token]}
    return len(present), len(expected), [token for token in needed if day not in traded.get(token, set())]


def day_is_complete(
    day: date,
    index_days: Sequence[date],
    traded: dict[str, set[date]],
    needed: Sequence[str] = (),
) -> bool:
    """Whether ``day`` can be processed: the universe is in, and so is every share held or planned.

    A held share stops being waited for once a later session exists, so a
    suspended one cannot hold up the desk for ever. The universe is always
    waited for: without it there is nothing to choose from.
    """
    present, expected, absent = missing_for_day(day, index_days, traded, needed)
    if not expected or present * 100 < expected * COMPLETE_PERCENT:
        return False
    return not absent or any(later > day for later in index_days)


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
