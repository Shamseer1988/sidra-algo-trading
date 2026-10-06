"""Rebuild the trading record from the broker's own reports.

For after a purge, a rebuild, or a restore from a backup that did not include
the trading tables. Upstox keeps the financial year; this pulls it back and
writes one ``broker_day_snapshots`` row per session that actually traded, which
is what the P&L calendar reads.

**What comes back, and what does not.** The broker reports matched buy/sell
pairs: scrip, quantity, buy average, sell average, and the two amounts — so
stock, quantity, entry, exit and gross are all recoverable, and so is the day's
total cost. The stop, the target, the risk amount, the strategy version and the
signal behind each trade are ours; they were in the tables that were cleared and
no API has them. A rebuilt day says so on the screen rather than passing itself
off as the audit record it is not.

**Read-only at the broker.** This runs through the report client, which has no
method that can place, modify or cancel an order. It cannot lose money.

**Cheap by construction.** The trades come back for the whole range in one
request. Only the charges have to be asked per day, because Upstox aggregates
them over whatever range it is given and the total cannot be split back out —
and a day whose charges have already settled is skipped, so re-running this
costs one request. A year of daily trading is about 250 requests against a
limit of 2,000 per thirty minutes.

Usage:

    python scripts/backfill_broker_history.py                  # current FY to date
    python scripts/backfill_broker_history.py --from 2026-09-01 --to 2026-10-06
    python scripts/backfill_broker_history.py --dry-run        # ask, record nothing
    python scripts/backfill_broker_history.py --force          # re-ask settled days
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import broker_day_figures as figures  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402


def financial_year_start(today: date) -> date:
    """1 April of the financial year ``today`` falls in."""
    return date(today.year if today.month >= 4 else today.year - 1, 4, 1)


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def main(args) -> int:
    settings = get_settings()
    today = datetime.now(MARKET_TIMEZONE).date()
    begin = args.from_date or financial_year_start(today)
    end = args.to_date or today

    if begin > end:
        print(f"  [PROBLEM] {begin} is after {end}. Nothing to do.")
        return 2
    if end > today:
        print(f"  [PROBLEM] {end} has not happened yet.")
        return 2

    from app.services.upstox_oauth import load_access_token
    from app.services.upstox_orders import UpstoxError, UpstoxReportClient, UpstoxSession

    token = await load_access_token(settings)
    if not token:
        print("  [PROBLEM] Upstox has no stored access token.")
        print("  Authorise it in the Upstox console first, then re-run.")
        return 2

    client = UpstoxReportClient(settings, UpstoxSession(access_token=token))

    print("=" * 70)
    print(f"REBUILDING from UPSTOX   {begin} to {end}")
    print("=" * 70)
    print("  Read-only at the broker. Local records are appended, never edited.")
    print()

    async with SessionLocal() as db:
        already = await figures.settled_dates(db, begin, end)
        if already and not args.force:
            print(f"  {len(already)} day(s) in this range have already settled and will be skipped.")

        if args.dry_run:
            # Trades only: no charges request, nothing written. Enough to answer
            # "what would this find" without spending a request per day.
            found: dict[date, int] = {}
            requests = 0
            for year, span_start, span_end in figures.financial_year_spans(begin, end):
                rows = await figures.fetch_rows(
                    client, span_start, span_end, financial_year_code=year, segment=args.segment
                )
                requests += 1
                grouped, unfiled = figures.group_by_session(rows)
                for session_date, day_rows in grouped.items():
                    if begin <= session_date <= end:
                        found[session_date] = len(day_rows)
                if unfiled:
                    print(f"  [WARN] {len(unfiled)} row(s) carried no readable date and were not filed.")
            print()
            for session_date in sorted(found):
                mark = "settled" if session_date in already else "would record"
                print(f"  {session_date}   {found[session_date]:>3} matched pair(s)   {mark}")
            print()
            print(f"  {len(found)} trading day(s) found in {requests} request(s). Nothing was written.")
            return 0

        try:
            report = await figures.sync_days(db, client, begin, end, segment=args.segment, force=args.force)
        except UpstoxError as error:
            print(f"  [PROBLEM] Upstox refused the request: {error}")
            print("  A rejected token or a financial year with no data both land here.")
            return 1
        await db.commit()

    for session_date in report.days_recorded:
        print(f"  recorded  {session_date}")
    for session_date in report.days_skipped:
        print(f"  skipped   {session_date}   already settled")
    if report.unfiled_rows:
        print(f"  [WARN] {report.unfiled_rows} row(s) carried no readable date and were not filed.")

    print()
    print(f"  {len(report.days_seen)} trading day(s) seen, {report.recorded} recorded.")
    print(f"  {report.requests} request(s) to Upstox.")
    if report.recorded:
        print()
        print("  Open Reports -> P&L calendar and switch the source to Broker.")
        print("  Rebuilt days are marked; they carry no stop, target or strategy.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session (YYYY-MM-DD).")
    parser.add_argument("--segment", default="EQ", help="EQ, FO, COM or CD. Default EQ.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be recorded, write nothing.")
    parser.add_argument("--force", action="store_true", help="Re-ask days whose charges have already settled.")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
