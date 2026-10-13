"""What history is actually stored, per session and per instrument.

A backtest is only as honest as its candles, and the two ways it lies quietly
are both invisible from its result: a session that is missing entirely, and a
session that is present but thin. Four stored sessions returned three trades
and looked like a strategy verdict.

So this counts what is there. A full NSE session is 375 one-minute candles
(09:15 to 15:30). Anything much below that is a feed gap, not a quiet day --
the scanner reports the same thing live as "N expected completed candle
bucket(s) are missing".

Read-only, and safe during a session.

Usage:

    docker compose exec api python scripts/candle_coverage.py
    docker compose exec api python scripts/candle_coverage.py --days 40
    docker compose exec api python scripts/candle_coverage.py --by-instrument
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import MarketCandle  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.trading_symbols import resolve_script_names  # noqa: E402

RULE = "=" * 78
FULL_SESSION = 375


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def bar(fraction: float, width: int = 24) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="How many recent sessions to list.")
    parser.add_argument("--timeframe", type=int, default=60, help="Candle timeframe in seconds.")
    parser.add_argument(
        "--by-instrument",
        action="store_true",
        help="Also show the per-instrument totals, which is where a single bad feed shows up.",
    )
    args = parser.parse_args()

    async with SessionLocal() as session:
        per_session = (
            await session.execute(
                select(
                    MarketCandle.session_date,
                    func.count(MarketCandle.id),
                    func.count(func.distinct(MarketCandle.instrument_token)),
                )
                .where(MarketCandle.timeframe_seconds == args.timeframe)
                .group_by(MarketCandle.session_date)
                .order_by(MarketCandle.session_date.desc())
                .limit(args.days)
            )
        ).all()
        totals = (
            await session.execute(
                select(
                    MarketCandle.instrument_token,
                    func.count(MarketCandle.id),
                    func.count(func.distinct(MarketCandle.session_date)),
                )
                .where(MarketCandle.timeframe_seconds == args.timeframe)
                .group_by(MarketCandle.instrument_token)
            )
        ).all()
        names = await resolve_script_names(session, {token for token, *_ in totals})

    if not per_session:
        print(f"\nNo {args.timeframe}s candles are stored at all. Nothing can be backtested.")
        print("  docker compose exec api python scripts/backfill_history.py --months 6")
        return 1

    sessions_held = len({day for day, *_ in per_session})
    heading(f"{args.timeframe}s CANDLES — {sessions_held} of the last {args.days} sessions stored")
    print(f"  {'session':<12}{'candles':>10}{'instruments':>13}{'per instrument':>16}   completeness")
    for day, candles, instruments in per_session:
        per_instrument = candles / instruments if instruments else 0
        print(
            f"  {day.isoformat():<12}{candles:>10,}{instruments:>13}{per_instrument:>16.0f}"
            f"   {bar(per_instrument / FULL_SESSION)} {per_instrument / FULL_SESSION * 100:5.1f}%"
        )

    if args.by_instrument:
        heading("PER INSTRUMENT")
        print(f"  {'instrument':<24}{'sessions':>9}{'candles':>10}{'per session':>13}")
        for token, candles, days in sorted(totals, key=lambda item: -item[1]):
            # Truncated, not left to overflow: an unresolved token is 20-odd
            # characters and pushes every number on its row out of line, which
            # is how a short session hides in a column that no longer lines up.
            label = names.get(token, token)[:22]
            print(f"  {label:<24}{days:>9}{candles:>10,}{candles / days if days else 0:>13.0f}")

    heading("WHAT THIS MEANS FOR A BACKTEST")
    full = sum(1 for _, candles, instruments in per_session if instruments and candles / instruments >= 350)
    print(f"  A full session is {FULL_SESSION} candles per instrument (09:15 to 15:30).")
    print(f"  Sessions at 350 or more: {full} of {len(per_session)} listed.")
    if sessions_held < 20:
        print()
        print("  Under about twenty sessions, a backtest result is an anecdote: a handful of")
        print("  trades whose win rate has a confidence interval wide enough to include both")
        print("  'excellent' and 'ruinous'. Load more before changing a setting on its word.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
