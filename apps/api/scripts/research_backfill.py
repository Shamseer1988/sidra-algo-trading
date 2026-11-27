"""Ask the research worker to build a historical dataset. Queues one job; fetches nothing itself.

    docker compose exec -T api python scripts/research_backfill.py \\
        --dataset nifty200-1m-2022 --timeframe 1minute --start 2022-01-01 --end 2026-11-26 \\
        --universe NIFTY200

    # or named instruments:
    ... --instrument "NSE_EQ|INE002A01018" --instrument "NSE_EQ|INE009A01021"

The worker fetches from Upstox's public history endpoint (no credentials), one
request per instrument and month, two a second -- about 12,000 requests and two
hours for the NIFTY 200 over five years -- and only outside 9:00 AM - 3:40 PM IST
on a trading day. When the window closes it stops between files; queue the same
command again and it continues where it stopped. NIFTY 50 is always included as
the benchmark that defines each session.

Writes one ``research_jobs`` row. Nothing else.
"""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import ResearchUniverseMembership  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import research_jobs  # noqa: E402
from app.services.research_data import TIMEFRAMES, month_ranges  # noqa: E402
from app.services.research_runs import BACKFILL  # noqa: E402

# Long enough for a five-year NIFTY 200 run at two requests a second.
BACKFILL_TIMEOUT_SECONDS = 6 * 60 * 60


async def instruments_of(universe: str) -> list[str]:
    async with SessionLocal() as session:
        rows = await session.scalars(
            select(ResearchUniverseMembership.instrument_key)
            .where(ResearchUniverseMembership.universe == universe, ResearchUniverseMembership.valid_to.is_(None))
            .distinct()
        )
        return sorted(rows.all())


async def queue(args: argparse.Namespace) -> int:
    instruments = list(args.instrument or [])
    if args.universe:
        instruments += await instruments_of(args.universe.upper())
    instruments = list(dict.fromkeys(instruments))
    if not instruments:
        print("No instruments. Pass --instrument, or --universe after research_import_universe.py.")
        return 1
    if args.start < date(2022, 1, 1) and args.timeframe != "day":
        print("Note: Upstox's public endpoint has no intraday bars before January 2022; earlier months will be empty.")
    requests = (len(instruments) + 1) * len(month_ranges(args.start, args.end))
    payload = {
        "dataset": args.dataset,
        "timeframe": args.timeframe,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "instruments": instruments,
        "requests_per_second": args.rate,
    }
    async with SessionLocal() as session:
        job = await research_jobs.enqueue(
            session, BACKFILL, payload, user_id=None, timeout_seconds=BACKFILL_TIMEOUT_SECONDS, max_attempts=3
        )
        await session.commit()
    print(f"Queued research job {job.id}: {len(instruments)} instruments + NIFTY 50, {args.timeframe},")
    print(
        f"{args.start} to {args.end}; about {requests:,} requests ({requests / args.rate / 3600:.1f} h at {args.rate}/s)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="A new, unique name, e.g. nifty200-1m-2022")
    parser.add_argument("--timeframe", required=True, choices=sorted(TIMEFRAMES))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--universe", help="Every current member of this research universe")
    parser.add_argument("--instrument", action="append", help="An instrument key; repeatable")
    parser.add_argument("--rate", type=float, default=2.0, help="Requests per second (default 2)")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end is before --start")
    return asyncio.run(queue(args))


if __name__ == "__main__":
    raise SystemExit(main())
