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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.db.models import MarketCandle  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.candle_aggregation import _bucket_start  # noqa: E402

RULE = "=" * 88
SOURCE_TIMEFRAME_SECONDS = 60


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@dataclass
class Bucket:
    """One coarse candle under construction, in source-candle order."""

    opened_at: datetime
    session_date: date
    instrument_token: str
    rows: list[MarketCandle] = field(default_factory=list)

    def add(self, row: MarketCandle) -> None:
        self.rows.append(row)

    def candle(self, timeframe_seconds: int) -> MarketCandle:
        ordered = sorted(self.rows, key=lambda row: row.opened_at)
        return MarketCandle(
            instrument_token=self.instrument_token,
            timeframe_seconds=timeframe_seconds,
            session_date=self.session_date,
            opened_at=self.opened_at,
            # The nominal close, not the last source candle's: a bucket missing
            # its final minute is still a candle of this timeframe, and a
            # closed_at that shrank with the gap would make the row describe a
            # timeframe that does not exist.
            closed_at=self.opened_at + timedelta(seconds=timeframe_seconds),
            open=ordered[0].open,
            high=max(row.high for row in ordered),
            low=min(row.low for row in ordered),
            close=ordered[-1].close,
            # Added, because a stored volume is already this candle's own:
            # CandleAggregationService._volume_delta turns the feed's running
            # total into a per-candle figure before the row is written. Adding
            # cumulative figures instead would have inflated every bucket
            # about threefold and left RVOL comparing against nonsense.
            volume=sum(row.volume for row in ordered),
            tick_count=sum(row.tick_count for row in ordered),
        )

    @property
    def minutes(self) -> int:
        return len(self.rows)


def buckets_for(rows: list[MarketCandle], timeframe_seconds: int) -> list[Bucket]:
    """Group source candles into coarse buckets, keyed the way live keys them."""
    grouped: dict[tuple[str, datetime], Bucket] = {}
    for row in rows:
        start = _bucket_start(row.opened_at, timeframe_seconds)
        key = (row.instrument_token, start)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = Bucket(opened_at=start, session_date=row.session_date, instrument_token=row.instrument_token)
            grouped[key] = bucket
        bucket.add(row)
    return [grouped[key] for key in sorted(grouped, key=lambda item: (item[0], item[1]))]


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

    async with SessionLocal() as session:
        query = select(MarketCandle).where(MarketCandle.timeframe_seconds == SOURCE_TIMEFRAME_SECONDS)
        if args.from_date:
            query = query.where(MarketCandle.session_date >= args.from_date)
        if args.to_date:
            query = query.where(MarketCandle.session_date <= args.to_date)
        if args.instruments:
            query = query.where(MarketCandle.instrument_token.in_(args.instruments))
        source = list((await session.scalars(query.order_by(MarketCandle.opened_at))).all())

        if not source:
            print("\n  No source candles matched. Nothing to do.")
            return 1

        sessions = sorted({row.session_date for row in source})
        instruments = sorted({row.instrument_token for row in source})
        print(f"  source              : {len(source):,} candles")
        print(f"  sessions            : {len(sessions)}   {sessions[0]} -> {sessions[-1]}")
        print(f"  instruments         : {len(instruments)}")

        built = buckets_for(source, target)
        expected = target // SOURCE_TIMEFRAME_SECONDS
        short = [bucket for bucket in built if bucket.minutes < expected]
        print(f"  {target}s candles to write : {len(built):,}")
        if short:
            # A short bucket is a real candle of a session with a feed gap, not
            # a mistake: it is reported rather than dropped, because dropping
            # it would quietly shorten those sessions in every replay.
            by_day: dict[date, int] = defaultdict(int)
            for bucket in short:
                by_day[bucket.session_date] += 1
            print(f"    short: {len(short):,} of them hold fewer than {expected} minutes")
            worst = sorted(by_day.items(), key=lambda item: -item[1])[:3]
            print("           most affected: " + ", ".join(f"{day} ({count})" for day, count in worst))
            print("           kept: a session with a feed gap is still that session.")

        existing = await session.scalar(
            select(MarketCandle.id).where(MarketCandle.timeframe_seconds == target).limit(1)
        )
        if existing is not None:
            print(f"\n  {target}s rows already exist and will be replaced for the sessions above.")

        if not args.apply:
            print("\n  Nothing was written. Add --apply to store these.")
            print(RULE)
            return 0

        await session.execute(
            delete(MarketCandle).where(
                MarketCandle.timeframe_seconds == target,
                MarketCandle.session_date.in_(sessions),
                MarketCandle.instrument_token.in_(instruments),
            )
        )
        session.add_all([bucket.candle(target) for bucket in built])
        await session.commit()
        print(f"\n  Wrote {len(built):,} candles at {target}s. Source rows untouched.")
        print(f"  Compare them with:  run_sweep.py --timeframe {target}   against   --timeframe 60")
        print(RULE)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
