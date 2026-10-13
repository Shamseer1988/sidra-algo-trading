"""Load past sessions of 1-minute candles, so the backtester has something to replay.

Four sessions of history is why a backtest over your own watchlist returns
three trades, and why no question about this system's settings has had an
answer: a score threshold, a reward:risk, a holding limit and a share universe
are all choices between distributions, and there has been no distribution.

This fetches whole past sessions from Upstox and stores raw OHLCV. It does not
run the live indicator pipeline, does not rewrite data-quality history, does
not publish snapshots and cannot produce a signal, an order or an alert from a
historical candle. Re-running it is safe: every candle is stored at most once,
so a run that fails halfway can simply be repeated.

Safe during a session, but it is a long job competing for the same database and
the same broker rate limit as the live scanner. Prefer running it after 15:30.

Usage:

    docker compose exec api python scripts/backfill_history.py --months 6
    docker compose exec api python scripts/backfill_history.py --from 2026-04-01 --to 2026-10-10
    docker compose exec api python scripts/backfill_history.py --months 1 --instrument NSE_EQ|INE423A01024

Upstox serves 1-minute candles a window at a time, so a long range is walked in
pieces; --window changes the size if a window is refused.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402
from app.services.upstox_backfill import RANGE_WINDOW_DAYS, backfill_intraday_range  # noqa: E402

RULE = "=" * 78


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=6, help="How far back to go. Ignored if --from is given.")
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session. Default: yesterday in IST.")
    parser.add_argument(
        "--instrument",
        action="append",
        dest="instruments",
        help="Limit to one instrument key; repeatable. Default: everything subscribed, plus the benchmark.",
    )
    parser.add_argument("--window", type=int, default=RANGE_WINDOW_DAYS, help="Days per request.")
    args = parser.parse_args()

    from app.core.config import get_settings
    from app.services.upstox_oauth import load_access_token

    settings = get_settings()
    # Yesterday by default: today's candles are the live feed's job, and asking
    # for a session that is still open returns a partial one.
    today = datetime.now(MARKET_TIMEZONE).date()
    to_date = args.to_date or today - timedelta(days=1)
    from_date = args.from_date or (to_date - timedelta(days=31 * max(args.months, 1)))

    token = await load_access_token(settings)
    if not token:
        print("No Upstox access token is stored. Renew the session first; nothing was fetched.")
        return 1

    heading(f"BACKFILL {from_date} -> {to_date}")
    print("  Raw candles only: no indicators, no quality history, no strategy evaluation.")
    print("  Safe to re-run; each candle is stored at most once.")

    # The calendar fails closed for a year it has no confirmed holiday list
    # for, which is right for trading and silent for a backfill: ask for a
    # range reaching into such a year and every candle from it is refused. Said
    # up front, because the alternative is a backtest quietly running on half
    # the history it was told it had.
    confirmed = set(settings.calendar_confirmed_years or {from_date.year})
    requested_years = set(range(from_date.year, to_date.year + 1))
    unconfirmed = sorted(requested_years - confirmed)
    if unconfirmed:
        print()
        print(f"  WARNING  The NSE calendar has no confirmed holiday list for {', '.join(map(str, unconfirmed))}.")
        print("           Candles from those years will be fetched and then refused, so this run")
        print("           will store nothing for them. Either narrow the range, or add the year to")
        print("           CALENDAR_CONFIRMED_YEARS with that year's holidays before relying on it.")
        print(f"           Years that will store: {', '.join(map(str, sorted(requested_years & confirmed))) or 'none'}")
    print()

    report = await backfill_intraday_range(
        settings,
        from_date=from_date,
        to_date=to_date,
        access_token=token,
        instruments=args.instruments,
        window_days=args.window,
    )

    heading("WHAT LANDED")
    print(f"  {'instrument':<34}{'sessions':>9}{'new':>10}{'already held':>14}{'refused':>9}")
    for instrument in sorted(report.stored):
        print(
            f"  {instrument:<34}{len(report.sessions.get(instrument, ())):>9}"
            f"{report.stored[instrument]:>10,}{report.already_present.get(instrument, 0):>14,}"
            f"{report.out_of_session.get(instrument, 0):>9,}"
        )
    print(f"\n  {report.total_stored:,} new candle(s) stored.")

    refused = sum(report.out_of_session.values())
    if refused:
        # Pre-open and post-close prints are refused on every normal run and
        # are a handful per session. Thousands means a whole year or a holiday
        # list the exchange does not agree with.
        print(
            f"  {refused:,} row(s) were refused as outside a confirmed exchange session. A few per "
            "session is\n  normal (pre-open and post-close prints); thousands means a year the "
            "calendar has not\n  confirmed, and nothing from it was stored."
        )

    if report.failures:
        heading("WINDOWS THAT FAILED")
        print("  A failed window is a gap, not a crash: re-run the same command and only")
        print("  the missing candles are fetched. Repeated HTTP 400s usually mean the")
        print("  window is wider than Upstox serves for 1-minute data -- try --window 10.")
        for instrument, details in sorted(report.failures.items()):
            print(f"\n  {instrument}")
            for detail in details[:8]:
                print(f"    {detail}")
            if len(details) > 8:
                print(f"    ... and {len(details) - 8} more")

    heading("NEXT")
    print("  Check coverage per session before trusting a backtest:")
    print("    docker compose exec api python scripts/candle_coverage.py")
    return 1 if report.failures and report.total_stored == 0 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
