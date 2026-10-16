"""One command, run once after the close, that says what the day did and whether
anything needs attention before the next one.

Written because the operator asked to stop guessing: they run this, paste the
output, and the decision is made from it. So it has to carry everything a
decision needs and nothing it does not, in an order that puts safety first.

  A  SAFETY      a position still open, or an order left unresolved
  B  THE DAY     each trade in R, how much of its target it collected, and when
  C  THE RECORD  the day against every live day so far
  D  THE CONFIG  whether any setting moved today, which decides whether the
                 record above is one configuration or several

Section B is the one that was missing. The live exit manager computes a reason
for every exit -- target, square-off, time -- and writes it to Telegram and to
the log, never to a row, so nothing could group by it. The first fifteen live
trades collected on average 62% of the reward they were sized for, and that,
not the win rate, is where the account's money went. The figures here are
derived from the fill, the planned levels and the clock, and are labelled as
derived.

Read-only. It opens one database session, runs selects, prints, and writes
nothing. Safe during a session, though it is meant for after the close.

Usage:

    python scripts/daily_review.py                 # today, IST
    python scripts/daily_review.py --date 2026-10-16
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import ApplicationSetting, LiveOrderSubmission, PaperPosition  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.execution_quality import (  # noqa: E402
    cost_of_trading,
    r_verdict,
    summarise_by_strategy,
    summarise_r,
)
from app.services.exit_quality import (  # noqa: E402
    LABELS,
    average_gross_r,
    average_reach,
    shape,
    tally,
)
from app.services.settings_history import changed_on, revision_history  # noqa: E402
from app.services.trade_counter import LIVE_PLACED_STATUSES, current_session_date, session_bounds_utc  # noqa: E402
from app.services.trade_history import LIVE, load_trades, summarise_days  # noqa: E402

RULE = "=" * 96
THIN = "-" * 96
# Every account setting that changes what a trade risks, how many are taken, or
# where they are left. A move in any of these splits the record either side of
# it into two configurations.
WATCHED = (
    "account_capital",
    "risk_per_trade_percent",
    "maximum_daily_risk_percent",
    "maximum_daily_trades",
    "maximum_open_positions",
    "minimum_rr",
    "stop_atr_multiple",
    "min_stop_distance_percent",
    "trade_start_time",
    "trade_cutoff_time",
    "session_square_off_time",
    "daily_loss_limit",
    "daily_profit_target",
    "universe_min_share_price",
    "universe_max_share_price",
    "live_entry_order_type",
    "entry_slippage_cap_percent",
    "entry_slippage_cap_r",
)


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def figure(value: Decimal | None, width: int = 8) -> str:
    return f"{'—':>{width}}" if value is None else f"{value:>{width},.2f}"


async def safety(session, day: date, records: list) -> int:  # noqa: ANN001
    """What must be looked at before the next session. Returns the alarm count."""
    heading(f"A. SAFETY — {day}")
    alarms = 0

    still_open = [record for record in records if record.is_open]
    if still_open:
        alarms += 1
        print(f"  [ALARM] {len(still_open)} position(s) are still open after the close:")
        for record in still_open:
            print(
                f"          {record.script_name}  {record.side}  {record.open_quantity} of "
                f"{record.quantity} still open  charges so far {money(record.charges)}"
            )
        print("          Either the broker still holds it, or the exit filled and the ledger did not")
        print(f"          record it. Check the broker's app, then inspect_live_day.py --date {day}.")
    else:
        print("  [OK]    Every position opened on this date is closed in the ledger.")

    start, end = session_bounds_utc(day)
    rows = (
        await session.scalars(
            select(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
    ).all()
    # An order the broker never answered about. Not the same as one that was
    # refused: a refusal is a decision, and this is a submission whose outcome
    # nobody established, which is the one state that can leave a position
    # unprotected without anything looking wrong.
    unresolved = [row for row in rows if row.status not in LIVE_PLACED_STATUSES and row.status != "REJECTED"]
    if unresolved:
        alarms += 1
        print(f"  [ALARM] {len(unresolved)} live order(s) have no settled outcome:")
        for row in unresolved:
            print(f"          {row.trading_symbol}  {row.price_type}  status {row.status}  {row.failure_message or ''}")
    placed = [row for row in rows if row.status in LIVE_PLACED_STATUSES]
    unfilled = [
        row
        for row in placed
        if (row.canonical_order_type or row.price_type or "").upper() == "LIMIT" and not row.filled_quantity
    ]
    if unfilled:
        print(f"  [NOTE]  {len(unfilled)} entry order(s) were placed and did not fill. They still consumed the")
        print("          day's risk allocation, because allocation is counted from what was sent.")
    print()
    print(f"  {len(placed)} live order(s) reached the broker on this date.")
    return alarms


def the_day(records: list, square_off) -> None:  # noqa: ANN001
    """Each closed trade in R, and how much of its plan it collected."""
    heading("B. THE DAY — what each trade did, in units of its own risk")
    closed = [record for record in records if not record.is_open]
    if not closed:
        print("  No trade closed on this date.")
        return

    shapes = [shape(record, square_off=square_off) for record in closed]
    print(
        f"  {'script':<14}{'strategy':<18}{'side':<6}{'gross R':>9}{'plan':>7}{'collected':>11}{'closed':>9}  left by"
    )
    for item in sorted(shapes, key=lambda row: row.closed_at or square_off or 0):
        collected = "—" if item.reach is None else f"{item.reach * 100:,.0f}%"
        print(
            f"  {item.script_name[:14]:<14}{item.strategy[:18]:<18}{item.side[:5]:<6}"
            f"{figure(item.gross_r, 9)}{figure(item.planned_rr, 7)}{collected:>11}"
            f"{(item.closed_at.strftime('%H:%M') if item.closed_at else '—'):>9}  {item.verdict}"
        )
    print(THIN)
    for reason, count in tally(shapes).items():
        print(f"  {count:>3} x {reason:<12} {LABELS[reason]}")

    won, lost = average_gross_r(shapes, wins=True), average_gross_r(shapes, wins=False)
    collected = average_reach(shapes)
    print()
    print(f"  average winner (gross)     : {figure(won, 1)}R")
    print(f"  average loser (gross)      : {figure(lost, 1)}R")
    if collected is not None:
        print(f"  winners collected          : {collected * 100:,.0f}% of the reward they were sized for")
    print("  Derived from the fill, the planned stop and target, and the clock. The live exit manager")
    print("  does not write its reason to a row, so 'left by' is inference, not the order that filled.")


async def the_record(session, day: date) -> None:
    """The day against every live day on record."""
    heading("C. THE RECORD — every live day so far")
    first = await session.scalar(select(func.min(PaperPosition.session_date))) or day
    records = await load_trades(session, first, day, execution_mode=LIVE)
    days = await summarise_days(session, records, first, day, execution_mode=LIVE)
    total = summarise_r(records, label="ALL")
    if not total.trades:
        print("  No closed live trades on record.")
        return

    print(f"  {first} to {day}, {len(days)} session(s)")
    print(f"  {'strategy':<20}{'trades':>7}{'won':>5}{'lost':>6}{'win%':>8}{'expectancy R':>15}{'net':>12}")
    for profile in [*summarise_by_strategy(records), total]:
        print(
            f"  {profile.label[:20]:<20}{profile.trades:>7}{profile.wins:>5}{profile.losses:>6}"
            f"{figure(profile.win_rate_percent, 8)}{figure(profile.expectancy_r, 15)}"
            f"{figure(profile.net_pnl, 12)}"
        )
    cost = cost_of_trading(days)
    print()
    print(f"  gross                      : {money(total.gross_pnl)}")
    print(f"  charges                    : {money(cost.total)}" + ("" if cost.is_complete else "  (partly estimated)"))
    print(f"  net                        : {money(total.net_pnl)}")
    print(f"  -> {r_verdict(total)}")
    if not cost.is_complete:
        named = ", ".join(str(item) for item in cost.estimated_days)
        print(f"     The broker has not billed {named}. Run backfill_broker_history.py once it has.")


async def the_config(session, day: date) -> int:
    """Whether the record above is one configuration or several."""
    heading("D. THE CONFIG — did anything move today")
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

    row = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls(**(row.value if row else DEFAULT_TRADING_CONTROLS))
    moved = changed_on(await revision_history(session, TRADING_KEY, limit=200), day, keys=WATCHED)
    if moved:
        print("  [NOTE]  Settings changed on this date: " + ", ".join(moved) + ".")
        print("          The day above was traded under more than one configuration, so it cannot be")
        print("          compared against days either side of it. One change per decision, not per day.")
    else:
        print("  [OK]    No setting that governs a trade moved on this date.")
    print()
    print(
        f"  risk per trade             : {controls.risk_per_trade_percent}% of {money(Decimal(str(controls.account_capital)))}"
    )
    print(
        f"  trades allowed             : {controls.maximum_daily_trades} a day, {controls.maximum_open_positions} open at once"
    )
    print(f"  stop floor / reward:risk   : {controls.min_stop_distance_percent}% / {controls.minimum_rr}:1")
    print(
        f"  window                     : {controls.trade_start_time} - {controls.trade_cutoff_time}, flat by {controls.session_square_off_time}"
    )
    return len(moved)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    args = parser.parse_args()
    day = args.date or current_session_date()

    async with SessionLocal() as session:
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls(**(row.value if row else DEFAULT_TRADING_CONTROLS))
        deadline = controls.session_square_off_time
        try:
            hour, minute = (int(part) for part in str(deadline).split(":"))
            square_off = datetime(2000, 1, 1, hour, minute).time()
        except (TypeError, ValueError):
            square_off = None

        heading(f"DAILY REVIEW — {day}")
        print("  Run after the close. Paste the whole output; the decision is made from it.")

        records = await load_trades(session, day, day, execution_mode=LIVE)
        alarms = await safety(session, day, records)
        the_day(records, square_off)
        await the_record(session, day)
        changes = await the_config(session, day)

    heading("WHAT THIS NEEDS")
    if alarms:
        print(f"  {alarms} thing(s) in section A need looking at before the next session.")
    else:
        print("  Nothing in section A. The ledger and the broker agree that the day is finished.")
    if changes:
        print("  A setting moved today, so this day stands apart from the ones around it.")
    print("  Nothing here was changed. This script cannot place, cancel or modify an order.")
    return 1 if alarms else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
