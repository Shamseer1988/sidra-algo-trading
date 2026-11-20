"""Read-only: how complete the swing desk's daily history is, and where it looks wrong.

Writes nothing. Safe any time.

    docker compose exec -T api python scripts/swing_data_coverage.py
    docker compose exec -T api python scripts/swing_data_coverage.py --years 2 --show-missing

For every NIFTY 200 share it reports how many daily candles are stored, the
first and last day, and which of the index's trading days in between have no
candle. Then it lists overnight drops that match a standard split or bonus
ratio. Those are questions, not answers: a confirmed one goes into
``corporate_events`` and is applied when the candles are read; a suspected one
is never applied.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78
# The strategies need 200 sessions of warm-up for the 200-day average, and the
# backtest more again before it has anything to test.
MIN_SESSIONS = 250


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--show-missing", action="store_true", help="List the missing days per share.")
    args = parser.parse_args()

    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.services.swing_data import confirmed_adjustments, coverage, load_daily, suspected_splits
    from app.services.swing_universe import nifty200

    settings = get_settings()
    to_date = datetime.now(MARKET_TIMEZONE).date()
    from_date = to_date - timedelta(days=365 * max(args.years, 1))
    members = nifty200()

    async with SessionLocal() as session:
        index = await load_daily(session, settings.upstox_nifty_benchmark_key, from_date=from_date)
        reference = [bar.session_date for bar in index]
        print(RULE + f"\nSWING DATA COVERAGE {from_date} -> {to_date}\n" + RULE)
        print(f"  NIFTY index: {len(reference)} trading day(s) stored -- the yardstick for every share.")
        if not reference:
            print("  No index history: run scripts/swing_backfill.py first. Nothing else can be judged.")
            return 1

        empty, gappy, short, complete = [], [], [], 0
        suspects = []
        confirmed_tokens = set()
        for member in members:
            bars = await load_daily(session, member.instrument_token, from_date=from_date)
            result = coverage(member.instrument_token, bars, reference)
            if result.sessions == 0:
                empty.append(member)
            elif result.missing:
                gappy.append((member, result))
            elif result.sessions < MIN_SESSIONS:
                short.append((member, result))
            else:
                complete += 1
            suspects.extend((member, suspect) for suspect in suspected_splits(bars))
            if await confirmed_adjustments(session, member.instrument_token):
                confirmed_tokens.add(member.instrument_token)

    print(f"\n  complete     : {complete} of {len(members)}")
    print(f"  with gaps    : {len(gappy)}")
    print(f"  too short    : {len(short)}  (fewer than {MIN_SESSIONS} sessions; the strategies cannot use them yet)")
    print(f"  no data      : {len(empty)}")
    if empty:
        print("\n  No daily candles at all:")
        for member in empty:
            print(f"    {member.symbol:<14} {member.instrument_token}")
    if short:
        print(f"\n  Too short to trade (under {MIN_SESSIONS} sessions -- re-run swing_backfill.py):")
        for member, result in sorted(short, key=lambda item: item[1].sessions):
            print(f"    {member.symbol:<14} {result.sessions:>4} sessions from {result.first}")
    if gappy:
        print("\n  Missing index trading days (from each share's own first candle):")
        for member, result in sorted(gappy, key=lambda item: -len(item[1].missing)):
            line = f"    {member.symbol:<14} {len(result.missing):>4} missing  first {result.first}  last {result.last}"
            print(line)
            if args.show_missing:
                print("      " + ", ".join(day.isoformat() for day in result.missing[:20]))

    print(RULE + "\nSUSPECTED SPLITS AND BONUSES (not applied until confirmed)\n" + RULE)
    if not suspects:
        print("  None found.")
    for member, suspect in suspects:
        status = "confirmed adjustment exists" if member.instrument_token in confirmed_tokens else "NOT confirmed"
        print(
            f"  {member.symbol:<14} {suspect.ex_date}  close {suspect.previous_close} -> open {suspect.open}"
            f"  x{suspect.jump} ~ {suspect.nearest_factor}   [{status}]"
        )
    print("\n  Wrote nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
