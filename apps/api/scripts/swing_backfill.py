"""Load daily candles for the swing desk's universe (NIFTY 200 plus the index).

Heavy, and only after 15:30 IST: about 200 shares x 2 years from the Upstox API.
It refuses to start on a weekday between 09:00 and 15:35 IST, because during a
session it would compete with the intraday scanner for the database and for the
broker's rate limit. Run it with nohup and a log file:

    nohup docker compose exec -T api python scripts/swing_backfill.py --years 2 \
        > logs/swing_backfill.log 2>&1 &

It writes daily candles only (``market_candles``, 86400-second rows). It never
rewrites a day already stored, so the intraday indicators' own 40 sessions are
untouched, and re-running it only fetches what is missing. Nothing here can
produce a signal, an order or an alert.

Options:
    --years N           how far back (default 2)
    --from / --to       an explicit range (YYYY-MM-DD)
    --instrument KEY    limit to one share; repeatable
    --limit N           only the first N shares of the list (for a trial run)
"""

import argparse
import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def market_hours_now(now: datetime) -> bool:
    """A weekday between 09:00 and 15:35 IST, when this job must not run."""
    return now.weekday() < 5 and time(9, 0) <= now.time() <= time(15, 35)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--from", dest="from_date", type=parse_day)
    parser.add_argument("--to", dest="to_date", type=parse_day)
    parser.add_argument("--instrument", action="append", dest="instruments")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    now = datetime.now(MARKET_TIMEZONE)
    if market_hours_now(now):
        print(f"Refusing to run at {now:%I:%M %p} IST on a trading weekday: this job competes with the")
        print("intraday scanner. Run it after 3:35 PM IST.")
        return 2

    from app.core.config import get_settings
    from app.services.swing_data import backfill_daily_range
    from app.services.swing_universe import nifty200
    from app.services.upstox_oauth import load_access_token

    settings = get_settings()
    to_date = args.to_date or now.date() - timedelta(days=1)
    from_date = args.from_date or to_date - timedelta(days=365 * max(args.years, 1))
    if args.instruments:
        tokens = list(args.instruments)
    else:
        members = nifty200()[: args.limit] if args.limit else nifty200()
        tokens = [settings.upstox_nifty_benchmark_key] + [member.instrument_token for member in members]

    access_token = await load_access_token(settings)
    if not access_token:
        print("No Upstox access token is stored. Renew the session first; nothing was fetched.")
        return 1

    print(RULE + f"\nSWING DAILY BACKFILL {from_date} -> {to_date}, {len(tokens)} instrument(s)\n" + RULE)
    report = await backfill_daily_range(tokens, from_date, to_date, access_token=access_token)

    print(RULE + "\nWHAT LANDED\n" + RULE)
    for token in tokens:
        print(f"  {token:<26} sessions {len(report.sessions.get(token, ())):>4}   new {report.stored.get(token, 0):>5}")
    print(f"\n  {report.total_stored:,} new daily candle(s) stored.")
    if report.failures:
        print(RULE + "\nWINDOWS THAT FAILED (re-run to fetch only what is missing)\n" + RULE)
        for token, details in sorted(report.failures.items()):
            print(f"  {token}: " + "; ".join(details[:4]))
    print("\nNext: python scripts/swing_data_coverage.py")
    return 1 if report.failures and report.total_stored == 0 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
