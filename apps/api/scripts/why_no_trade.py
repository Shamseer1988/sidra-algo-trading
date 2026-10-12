"""Why the scanner did not take another trade today.

"It traded once and then stopped" has several possible answers and they live
in different places: the day's trade ceiling, the one-position rule, the score
threshold, the required inputs, or simply no setup forming. Guessing between
them is how a working system gets "fixed".

Every evaluation the scanner makes is recorded with the reason it stopped
there, so the answer is already stored. This groups them.

Read-only, and safe during a session.

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

from app.db.models import ScannerEvaluation  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402
from app.services.trading_symbols import resolve_script_names  # noqa: E402

RULE = "=" * 78


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


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
            f"    {ist(row).strftime('%H:%M')}  {names.get(row.instrument_token, row.instrument_token)}  "
            f"{row.side or ''}  score {row.score}"
        )

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
                f"score {row.score:>3}  {row.reason}"
            )
    else:
        heading("CLOSEST THINGS TO A SIGNAL")
        print("  Nothing was scored at all: no setup reached the point of being judged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
