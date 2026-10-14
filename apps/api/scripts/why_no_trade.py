"""Why the scanner did not take another trade today.

"It traded once and then stopped" has several possible answers and they live
in different places: the day's trade ceiling, the one-position rule, the score
threshold, the required inputs, or simply no setup forming. Guessing between
them is how a working system gets "fixed".

Every evaluation the scanner makes is recorded with the reason it stopped
there, so the answer is already stored. This groups them.

It also follows each accepted signal onto the live path, because the
evaluations alone could not answer the question on 14 October: five signals
were accepted, one live order was sent, and the reason was two gates further
down -- an in-flight submission and the paper engine's own ceiling. A section
that stops at "accepted" sends the operator to the logs for the half of the
story that matters.

Read-only, and safe during a session. The broker is not called; the live half
is read from this system's own live records, so the open position count and
exposure are the two live caps it cannot report. ``inspect_live_day.py`` asks
the broker for those.

Usage:

    docker compose exec api python scripts/why_no_trade.py
    docker compose exec api python scripts/why_no_trade.py --date 2026-10-12
    docker compose exec api python scripts/why_no_trade.py --after 11:11
"""

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import LiveOrderSubmission, PaperSignal, ScannerEvaluation  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.live_usage import cap_lines, live_usage  # noqa: E402
from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402
from app.services.trade_counter import session_bounds_utc  # noqa: E402
from app.services.trading_symbols import resolve_script_names  # noqa: E402

RULE = "=" * 78


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


# Shorthand for what a live submission's status means for the account. Grouped
# rather than printed raw because PREPARED and UNKNOWN look like failures and
# are not: one is an order mid-flight and the other an order whose outcome was
# never learned, which may well be resting at the broker.
AT_BROKER = ("ACCEPTED", "RESOLVED_PLACED")
UNRESOLVED = ("PREPARED", "UNKNOWN", "NEEDS_REVIEW")


def report_live_path(day, signals, submissions, names) -> None:  # noqa: ANN001
    """Each signal the scanner accepted, and what the live path did with it.

    The gap this closes: on 14 October the evaluations said five signals were
    accepted and the account held one trade. Everything in between -- the
    paper engine's refusal, the in-flight gate, a live gate -- was only in the
    logs, so "why no second trade" cost a transcript to answer.
    """
    heading("WHAT THE LIVE PATH DID WITH EACH ACCEPTED SIGNAL")
    if not signals:
        print("  No signals were recorded, so the live path was never offered one.")
        return

    by_signal: dict[object, list] = {}
    orphans = []
    for row in submissions:
        if row.paper_signal_id is None:
            orphans.append(row)
        else:
            by_signal.setdefault(row.paper_signal_id, []).append(row)

    print(f"  {'time':<7}{'instrument':<13}{'side':<6}{'paper':<22}{'live':<24}risk")
    for signal in signals:
        rows = by_signal.get(signal.id, [])
        at_broker = [row for row in rows if row.status in AT_BROKER]
        unresolved = [row for row in rows if row.status in UNRESOLVED]
        if at_broker:
            live = f"{len(at_broker)} order(s) at broker"
        elif unresolved:
            live = f"{unresolved[0].status.lower()}, unresolved"
        elif rows:
            live = f"{rows[0].status.lower()}"
        else:
            live = "no live order"
        planned = next((row.planned_risk for row in rows if row.planned_risk is not None), None)
        when = signal.candle_opened_at.astimezone(MARKET_TIMEZONE).time().strftime("%H:%M")
        name = names.get(signal.instrument_token, signal.instrument_token)[:12]
        print(
            f"  {when:<7}{name:<13}{(signal.side or ''):<6}{signal.status[:21]:<22}{live:<24}"
            f"{(f'{planned:,.2f}' if planned is not None else '-')}"
        )
        # The paper engine's reason, stored on the signal when it refused. It
        # no longer withholds the live order, so it is printed as context
        # rather than as the answer.
        if signal.status == "PAPER_RISK_REJECTED" and signal.alert_detail:
            print(f"           paper refused: {signal.alert_detail}")

    if orphans:
        print()
        print(f"  {len(orphans)} live order(s) carry no signal: ", end="")
        print(", ".join(sorted({row.trading_symbol for row in orphans})))
        print("  Those are exits and protective stops, or an order placed by hand.")
    print()
    print("  'paper' is the paper journal's verdict and 'live' is the account's. A paper")
    print("  refusal withholds the paper order only -- the live path has its own caps now,")
    print("  and the section below is those.")


def report_live_caps(usage) -> None:  # noqa: ANN001
    """The live account's own caps, and what today has used against them.

    Formatted by ``live_usage.cap_lines`` and computed by the gate's own
    arithmetic, so a number here disagreeing with a refusal in the logs is a
    bug rather than a rounding.
    """
    heading("THE LIVE ACCOUNT'S OWN CAPS, AND TODAY'S USE OF THEM")
    for text in cap_lines(usage):
        print(f"  {text}" if text else "")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    parser.add_argument("--after", type=parse_clock, help="Only count evaluations after this IST time, e.g. 11:11.")
    args = parser.parse_args()

    day = args.date or datetime.now(MARKET_TIMEZONE).date()
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(ScannerEvaluation)
                    .where(ScannerEvaluation.session_date == day)
                    .order_by(ScannerEvaluation.candle_opened_at)
                )
            ).all()
        )
        if not rows:
            print(f"No evaluations recorded for {day}. The scanner did not run, or the day is not a trading day.")
            return 1
        names = await resolve_script_names(session, {row.instrument_token for row in rows})
        usage = await live_usage(session, session_date=day)
        start, end = session_bounds_utc(day)
        signals = list(
            (
                await session.scalars(
                    select(PaperSignal).where(PaperSignal.session_date == day).order_by(PaperSignal.candle_opened_at)
                )
            ).all()
        )
        submissions = list(
            (
                await session.scalars(
                    select(LiveOrderSubmission)
                    .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
                    .order_by(LiveOrderSubmission.created_at)
                )
            ).all()
        )

    def ist(row) -> time:
        return row.candle_opened_at.astimezone(MARKET_TIMEZONE).time()

    considered = [row for row in rows if args.after is None or ist(row) >= args.after]
    window = f" after {args.after.strftime('%H:%M')}" if args.after else ""

    heading(f"{day}{window} — {len(considered):,} evaluations")
    accepted = [row for row in considered if row.status == "ACCEPTED"]
    print(
        f"  accepted {len(accepted)}   watching {sum(r.status not in ('ACCEPTED', 'REJECTED') for r in considered)}"
        f"   rejected {sum(r.status == 'REJECTED' for r in considered)}"
    )
    for row in accepted:
        print(
            f"    {ist(row).strftime('%H:%M')}  {names.get(row.instrument_token, row.instrument_token):<12} "
            f"{row.side or '':<6} score {row.score:>3}  {row.strategy_name}"
        )

    # Which strategy is doing the work, which is not answerable from the
    # reason counts: four strategies share most of their refusal wording, and
    # turning one off or on is a decision about where the signals come from.
    # 8 October ran four and accepted 8; the three that were disabled after it
    # had been producing most of the opportunity, and nothing said which.
    heading("WHO IS WORKING — per strategy, over this window")
    by_strategy: dict[str, Counter] = {}
    for row in considered:
        counts = by_strategy.setdefault(row.strategy_name, Counter())
        counts[row.status] += 1
        counts["total"] += 1
        if row.score:
            counts["scored"] += 1
            counts["score_sum"] += int(row.score)
    print(f"  {'strategy':<26}{'evaluations':>12}{'accepted':>10}{'reached scoring':>17}{'best score':>12}")
    best: dict[str, int] = {}
    for row in considered:
        if row.score:
            best[row.strategy_name] = max(best.get(row.strategy_name, 0), int(row.score))
    for name, counts in sorted(by_strategy.items(), key=lambda item: -item[1]["total"]):
        print(
            f"  {name[:25]:<26}{counts['total']:>12,}{counts.get('ACCEPTED', 0):>10}"
            f"{counts.get('scored', 0):>17,}{best.get(name, 0):>12}"
        )
    print()
    print("  'reached scoring' is the one to read: a strategy whose setups never get as far")
    print("  as a score is not being refused by the threshold, it is not finding setups.")

    heading("WHY EACH ONE STOPPED, most common first")
    for reason, count in Counter(row.reason for row in considered).most_common(12):
        print(f"  {count:>7,}  {reason}")

    # A setup that formed and was turned away on its score is a different
    # story from no setup forming at all, and the second is far more common.
    near = sorted((row for row in considered if row.score and row.status != "ACCEPTED"), key=lambda row: -row.score)[:8]
    if near:
        heading("CLOSEST THINGS TO A SIGNAL — scored, but not taken")
        for row in near:
            print(
                f"  {ist(row).strftime('%H:%M')}  {names.get(row.instrument_token, row.instrument_token):<12} "
                f"score {row.score:>3}  {row.strategy_name[:22]:<23} {row.reason}"
            )
    else:
        heading("CLOSEST THINGS TO A SIGNAL")
        print("  Nothing was scored at all: no setup reached the point of being judged.")

    report_live_path(day, signals, submissions, names)
    report_live_caps(usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
