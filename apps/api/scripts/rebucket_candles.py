"""Build a coarser timeframe out of the one-minute candles already stored.

Switching the scanning timeframe is the one setting change that cannot be made
and then measured, because the measurement needs history at the new timeframe
and there is none. Upstox's intraday history endpoint this app uses is fixed at
``1minute`` (``upstox_backfill.UPSTOX_RANGE_URL``), so the coarser candles
cannot be fetched -- but they do not need to be. A three-minute candle is
exactly three one-minute candles: the first one's open, the last one's close,
the highest high, the lowest low, and the volumes added up. Six months of
minutes are already in ``market_candles``.

So this reads them and writes the coarser rows beside them. The source rows are
never touched, which is what makes the comparison possible at all:

    run_sweep.py --timeframe 60     # what the scanner trades today
    run_sweep.py --timeframe 180    # what it would trade instead

Two replays of the same sessions, one answer. Nothing live changes until
``candle_timeframe_seconds`` is changed, and that is a separate decision taken
on the result rather than in advance of it.

Buckets come from ``candle_aggregation._bucket_start``, the same function the
live aggregator buckets ticks with. Written out by hand the two would agree
until a daylight assumption or a rounding direction differed, and then the
history would be measuring a timeframe the scanner never produces.

    docker compose exec api python scripts/rebucket_candles.py
    docker compose exec api python scripts/rebucket_candles.py --apply
    docker compose exec api python scripts/rebucket_candles.py --timeframe 300 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db.models import MarketCandle  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.candle_aggregation import _bucket_start, fold_to_timeframe  # noqa: E402
from app.services.market_calculations import CompletedCandle  # noqa: E402

RULE = "=" * 88
SOURCE_TIMEFRAME_SECONDS = 60


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def as_completed(row: MarketCandle) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=row.instrument_token,
        timeframe_seconds=row.timeframe_seconds,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        tick_count=row.tick_count,
    )


def as_row(candle: CompletedCandle) -> MarketCandle:
    return MarketCandle(
        instrument_token=candle.instrument_token,
        timeframe_seconds=candle.timeframe_seconds,
        session_date=candle.session_date,
        opened_at=candle.opened_at,
        closed_at=candle.closed_at,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        tick_count=candle.tick_count,
    )


def minutes_per_bucket(rows: list[MarketCandle], timeframe_seconds: int) -> dict[tuple[str, datetime], int]:
    """How many source candles each bucket holds, to report the short ones.

    ``fold_to_timeframe`` keeps a short bucket, as it should -- a session with
    a feed gap is still that session -- so the count is taken here rather than
    inferred from the folded rows, where the gap no longer shows.
    """
    counts: dict[tuple[str, datetime], int] = defaultdict(int)
    for row in rows:
        counts[(row.instrument_token, _bucket_start(row.opened_at, timeframe_seconds))] += 1
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--timeframe",
        type=int,
        default=180,
        help="Seconds per candle to build. Must be a whole number of minutes above 60. Default 180.",
    )
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session (YYYY-MM-DD).")
    parser.add_argument("--instrument", action="append", dest="instruments", help="Limit instruments; repeatable.")
    parser.add_argument("--apply", action="store_true", help="Write the rows. Without this nothing is stored.")
    args = parser.parse_args()

    target = args.timeframe
    if target <= SOURCE_TIMEFRAME_SECONDS or target % SOURCE_TIMEFRAME_SECONDS:
        print(f"--timeframe must be a whole number of minutes above {SOURCE_TIMEFRAME_SECONDS}; got {target}.")
        return 1

    print(RULE)
    print("REBUCKET CANDLES" + ("" if args.apply else "   (report only)"))
    print(RULE)
    print(f"  {SOURCE_TIMEFRAME_SECONDS}s candles in -> {target}s candles out, {target // 60} minutes each.")
    print("  The source rows are left exactly as they are.")

    def bounded(query):
        if args.from_date:
            query = query.where(MarketCandle.session_date >= args.from_date)
        if args.to_date:
            query = query.where(MarketCandle.session_date <= args.to_date)
        if args.instruments:
            query = query.where(MarketCandle.instrument_token.in_(args.instruments))
        return query

    # One instrument at a time, and never the whole table.
    #
    # The first version read every matching row into a list and folded that.
    # Against six months of 29 instruments -- 1,375,887 candles -- the process
    # was killed before it printed another line: an ORM row is not a cheap
    # object, and there were three copies of the history alive at once (rows,
    # CompletedCandles, the buckets holding them). A silent SIGKILL looks
    # exactly like a script that decided there was nothing to do, which is the
    # worst way for this to fail.
    #
    # Instrument is the right unit because a bucket never spans two of them, so
    # folding one instrument's history is the same answer as folding all of it,
    # and the peak is one instrument rather than twenty-nine.
    async with SessionLocal() as session:
        instruments = list(
            (
                await session.scalars(
                    bounded(
                        select(MarketCandle.instrument_token)
                        .where(MarketCandle.timeframe_seconds == SOURCE_TIMEFRAME_SECONDS)
                        .distinct()
                    ).order_by(MarketCandle.instrument_token)
                )
            ).all()
        )
        if not instruments:
            print("\n  No source candles matched. Nothing to do.")
            return 1

        span = (
            await session.execute(
                bounded(
                    select(
                        func.count(MarketCandle.id),
                        func.min(MarketCandle.session_date),
                        func.max(MarketCandle.session_date),
                        func.count(MarketCandle.session_date.distinct()),
                    ).where(MarketCandle.timeframe_seconds == SOURCE_TIMEFRAME_SECONDS)
                )
            )
        ).one()
        total, first_day, last_day, session_count = span
        print(f"  source              : {total:,} candles")
        print(f"  sessions            : {session_count}   {first_day} -> {last_day}")
        print(f"  instruments         : {len(instruments)}")
        print()

        expected = target // SOURCE_TIMEFRAME_SECONDS
        written = 0
        short_total = 0
        short_by_day: dict[date, int] = defaultdict(int)

        for index, token in enumerate(instruments, start=1):
            rows = list(
                (
                    await session.scalars(
                        bounded(
                            select(MarketCandle).where(
                                MarketCandle.timeframe_seconds == SOURCE_TIMEFRAME_SECONDS,
                                MarketCandle.instrument_token == token,
                            )
                        ).order_by(MarketCandle.opened_at)
                    )
                ).all()
            )
            built = fold_to_timeframe([as_completed(row) for row in rows], target)
            counts = minutes_per_bucket(rows, target)
            for candle in built:
                if counts[(candle.instrument_token, candle.opened_at)] < expected:
                    short_total += 1
                    short_by_day[candle.session_date] += 1
            written += len(built)
            print(f"  [{index:>2}/{len(instruments)}] {token:<28} {len(rows):>7,} -> {len(built):>7,}")

            if args.apply:
                await session.execute(
                    bounded(
                        delete(MarketCandle).where(
                            MarketCandle.timeframe_seconds == target,
                            MarketCandle.instrument_token == token,
                        )
                    )
                )
                session.add_all([as_row(candle) for candle in built])
                # Committed per instrument so a run that dies part way leaves
                # whole instruments done rather than nothing, and so the rows
                # just built can be released.
                await session.commit()

            # The ORM keeps every row it loaded in its identity map, which would
            # defeat reading one instrument at a time.
            session.expunge_all()
            rows.clear()
            built.clear()

        print()
        print(f"  {target}s candles {'written' if args.apply else 'to write'} : {written:,}")
        if short_total:
            # A short bucket is a real candle of a session with a feed gap, not
            # a mistake: it is reported rather than dropped, because dropping
            # it would quietly shorten those sessions in every replay.
            print(f"    short: {short_total:,} of them hold fewer than {expected} minutes")
            worst = sorted(short_by_day.items(), key=lambda item: -item[1])[:3]
            print("           most affected: " + ", ".join(f"{day} ({count})" for day, count in worst))
            print("           kept: a session with a feed gap is still that session.")

        if not args.apply:
            print("\n  Nothing was written. Add --apply to store these.")
            print(RULE)
            return 0

        print("\n  Source rows untouched.")
        print(f"  Compare them with:  run_sweep.py --timeframe {target}   against   --timeframe 60")
        print(RULE)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
