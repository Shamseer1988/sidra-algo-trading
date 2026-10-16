"""Whether each strategy is worth running, in the only units that answer it.

The app reports a strategy's net P&L. That is the result, not the verdict: a
win rate is neither good nor bad on its own, it is good or bad against the win
rate the reward:risk needs. 33% is a comfortable win rate at 3:1 and a losing
one at 1.6:1, and nothing on any screen put the two side by side.

Three decisions shape everything printed below.

**R, not rupees.** R is the net result divided by the risk planned for that
trade. Risk per trade moved from 100 to 50 between two consecutive sessions
here, so a money average over both is weighted towards the larger-risk half and
reads as a statement about the strategy when it is partly a statement about the
sizing. Every per-trade figure is in R; the money is reported as totals, where
it is unambiguous.

**Grouped by strategy, not by saved version.** ``strategy_version`` is
``type@version`` and the version is bumped on every settings save -- ORB went
v12 to v15 in three days of unchanged logic. Grouping by the full string
scatters one strategy across a row per save, each too small to read.

**The two ledgers never mix.** A paper trade cost nothing and filled perfectly.
Averaging one into the live record dilutes every figure with a trade that could
not have happened.

Read-only. It opens one database session, runs selects, and writes nothing.

Usage:

    python scripts/strategy_stats.py                        # everything on record
    python scripts/strategy_stats.py --from 2026-10-08
    python scripts/strategy_stats.py --ledger live
"""

import argparse
import asyncio
import sys
import textwrap
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import PaperPosition  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.execution_quality import (  # noqa: E402
    RProfile,
    cost_of_trading,
    r_verdict,
    read_verdict,
    summarise_by_strategy,
    summarise_expectancy,
    summarise_r,
)
from app.services.trade_counter import current_session_date  # noqa: E402
from app.services.trade_history import LIVE, PAPER, TradeRecord, load_trades, summarise_days  # noqa: E402

RULE = "=" * 98
THIN = "-" * 98
WIDTH = 96
# Where the value starts on a "label : value" line, so a figure explaining one
# can be put under it rather than beside it.
VALUE_COLUMN = 31


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def paragraph(text: str, *, indent: str = "  ", hanging: str | None = None) -> None:
    """Wrapped to the terminal, because these sentences are the point.

    The sample caveat and the verdict are the two things on this screen a reader
    must not skim, and a 250-character line wrapped by the terminal mid-number is
    skimmed.
    """
    for line in textwrap.wrap(text, width=WIDTH, initial_indent=indent, subsequent_indent=hanging or indent):
        print(line)


def number(value: Decimal | None, width: int = 10, places: str = ",.2f") -> str:
    """A figure, or a dash. Never a zero standing in for one that is absent."""
    return f"{'-':>{width}}" if value is None else f"{value:>{width}{places}}"


def count_row(profile: RProfile) -> str:
    return (
        f"  {profile.label[:22]:<22}{profile.trades:>7}{profile.wins:>6}{profile.losses:>6}"
        f"{profile.scratches:>5}{number(profile.win_rate_percent, 9, ',.1f')}"
        f"{number(profile.average_r_won, 12)}{number(profile.average_r_lost, 12)}"
        f"{number(profile.expectancy_r, 15)}"
    )


def money_row(profile: RProfile) -> str:
    return (
        f"  {profile.label[:22]:<22}{number(profile.gross_pnl, 14)}{number(profile.charges, 16)}"
        f"{number(profile.net_pnl, 14)}{f'{profile.measured} of {profile.trades}':>14}"
    )


def strategy_rows(profiles: list[RProfile], total: RProfile) -> None:
    """Counts and R first, money second: two tables rather than one too wide."""
    print(
        f"  {'strategy':<22}{'trades':>7}{'won':>6}{'lost':>6}{'scr':>5}{'win%':>9}{'avg R won':>12}"
        f"{'avg R lost':>12}{'expectancy R':>15}"
    )
    for profile in profiles:
        print(count_row(profile))
    print(THIN)
    print(count_row(total))
    print()
    print(f"  {'strategy':<22}{'gross':>14}{'charges (est)':>16}{'net':>14}{'R measured':>14}")
    for profile in profiles:
        print(money_row(profile))
    print(THIN)
    print(money_row(total))


def sizing_note(records: list[TradeRecord]) -> str | None:
    """Said only when it is true: the risk per trade changed inside this range.

    This is the whole reason the table above is in R. An operator who has not
    changed the sizing does not need the paragraph.
    """
    sizes = sorted({record.risk_amount for record in records if record.risk_amount > 0})
    if len(sizes) < 2:
        return None
    return (
        f"Risk per trade was not constant in this range: {', '.join(f'{size:,.0f}' for size in sizes)}. "
        "The money totals mix those sizes; the R figures do not, which is why the verdict is read off R."
    )


async def report(session, ledger: str, from_date: date, to_date: date) -> int:  # noqa: ANN001
    """One ledger's section. Returns the number of closed trades it found."""
    records = await load_trades(session, from_date, to_date, execution_mode=ledger)
    days = await summarise_days(session, records, from_date, to_date, execution_mode=ledger)
    closed = [record for record in records if not record.is_open]
    total = summarise_r(records, label="ALL")
    profiles = summarise_by_strategy(records)

    title = f"{ledger} — real orders, real charges" if ledger == LIVE else f"{ledger} — simulated, for comparison"
    heading(title)
    if not closed:
        print(f"  No closed {ledger.lower()} trades between {from_date} and {to_date}.")
        open_now = len(records) - len(closed)
        if open_now:
            print(f"  {open_now} position(s) are still open and are excluded: an open trade has no result yet.")
        return 0

    strategy_rows(profiles, total)

    print()
    print(f"  sessions traded            : {len(days)}")
    print(f"  gross                      : {total.gross_pnl:,.2f}")

    if ledger == LIVE:
        cost = cost_of_trading(days)
        print(f"  charges                    : {cost.total:,.2f}")
        under = " " * VALUE_COLUMN
        if cost.settled_days:
            print(f"{under}{cost.settled:,.2f} billed by the broker over {cost.settled_days} settled day(s)")
        if cost.estimated_days:
            named = ", ".join(str(day) for day in cost.estimated_days)
            print(f"{under}{cost.estimated:,.2f} this system's estimate for {len(cost.estimated_days)} day(s)")
            paragraph(
                f"The broker has not billed {named}. Until it does, the cost of trading above is partly an "
                "estimate and reads a little low \u2014 the error always points that way. "
                "backfill_broker_history.py fetches a day once it settles.",
                indent=under,
            )
    else:
        print(f"  charges                    : {total.charges:,.2f}  (estimate; nothing here was billed)")

    print(f"  net                        : {total.net_pnl:,.2f}")
    if total.break_even_win_rate_percent is not None:
        at = f" at the {total.reward_risk}:1 it actually achieved" if total.reward_risk is not None else ""
        print(f"  break-even win rate        : {total.break_even_win_rate_percent}%{at}")
    print()
    paragraph(f"-> {r_verdict(total)}", hanging="     ")
    if len(profiles) > 1:
        for profile in profiles:
            paragraph(f"{profile.label[:22]}: {r_verdict(profile)}", indent="     ", hanging="       ")
    print()
    paragraph(read_verdict(summarise_expectancy(records), ledger=ledger.lower()))

    note = sizing_note(closed)
    if note:
        print()
        paragraph(note)
    if total.measured < total.trades:
        print()
        paragraph(
            f"{total.trades - total.measured} closed trade(s) carry no planned risk, so they have no R and are "
            "absent from every R figure above. They are counted in the money totals."
        )
    if ledger == LIVE:
        print()
        paragraph(
            "net above is gross less this system's per-trade estimate. Neither Upstox nor Firstock bills "
            "per trade, so a settled day's figure can be compared against the day and never pushed back "
            "into a trade -- which is why the charges line and the net can disagree."
        )
    brokers = sorted({record.broker for record in closed if record.broker})
    if len(brokers) > 1:
        print()
        paragraph(f"Two accounts are mixed here: {', '.join(brokers)}. A total over both is true of neither.")
    return len(closed)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session. Default: today in IST.")
    parser.add_argument(
        "--ledger",
        choices=("live", "paper", "both"),
        default="both",
        help="Which record to report. Default: both, reported separately.",
    )
    args = parser.parse_args()

    async with SessionLocal() as session:
        to_date = args.to_date or current_session_date()
        from_date = args.from_date
        if from_date is None:
            # Every day on record. A default of "the last 30 days" would quietly
            # drop the earliest sessions, which on a six-session history is most
            # of the evidence.
            from_date = await session.scalar(select(func.min(PaperPosition.session_date))) or to_date

        heading(f"Strategy statistics, {from_date} to {to_date}")
        print("  Every per-trade figure is in R: the trade's net result divided by the risk planned")
        print("  for it. 1R won is a trade that made what it was prepared to lose. Net is after this")
        print("  system's charge estimate throughout. Open positions are excluded everywhere.")

        found = 0
        for ledger in (LIVE, PAPER):
            if args.ledger in ("both", ledger.lower()):
                found += await report(session, ledger, from_date, to_date)

    heading("SUMMARY")
    if not found:
        print(f"  Nothing closed between {from_date} and {to_date}.")
    else:
        print(f"  {found} closed trade(s) read. Nothing here was changed.")
    print()
    print("  Not covered: whether the strategies were run on comparable days. A verdict over a range")
    print("  in which the settings changed is a verdict on several configurations at once, and this")
    print("  cannot see which day carried which. check_settings.py shows what is live now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
