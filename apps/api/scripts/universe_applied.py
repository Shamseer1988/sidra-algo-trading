"""Did the watchlist ranking actually govern what was scanned today?

The ranking can be right and still not be applied. On 12 October it ranked 27
instruments, selected 21, and all 27 were scanned anyway: the scanner's first
look happened at 09:15, before the 09:25 refresh had written anything, and it
cached that empty answer for the rest of the day. The ranking was correct, the
selection was correct, and the filter did nothing -- which no screen showed,
because every screen reported the ranking rather than what the scanner did
with it.

So this compares the two directly: what the ranking selected, against which
instruments actually produced evaluation rows. Any instrument in the second
list and not the first is a filter that is not being applied.

Read-only, and safe during a session.

Usage:

    docker compose exec api python scripts/universe_applied.py
    docker compose exec api python scripts/universe_applied.py --date 2026-10-12
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import ScannerEvaluation, ScanUniverseEntry  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402
from app.services.trading_symbols import resolve_script_names  # noqa: E402
from app.services.universe import ranking_is_on  # noqa: E402

RULE = "=" * 78
OK, WARN, BAD = "[OK]  ", "[WARN]", "[BAD] "


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def listed(names: dict[str, str], tokens) -> str:
    return ", ".join(sorted(names.get(token, token) for token in tokens))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    args = parser.parse_args()
    day = args.date or datetime.now(MARKET_TIMEZONE).date()

    from app.core.config import get_settings

    settings = get_settings()
    on = await ranking_is_on(settings)

    async with SessionLocal() as session:
        entries = list(
            (await session.scalars(select(ScanUniverseEntry).where(ScanUniverseEntry.session_date == day))).all()
        )
        scanned = set(
            (
                await session.scalars(
                    select(ScannerEvaluation.instrument_token).distinct().where(ScannerEvaluation.session_date == day)
                )
            ).all()
        )
        ranked = {entry.instrument_token for entry in entries}
        selected = {entry.instrument_token for entry in entries if entry.selected}
        names = await resolve_script_names(session, ranked | scanned)

    heading(f"{day} — did the ranking govern the scan?")
    print(f"  ranking enabled            : {on}")
    print(f"  ranked                     : {len(ranked)}")
    print(f"  selected by the ranking    : {len(selected)}")
    print(f"  actually scanned           : {len(scanned)}")

    if not on:
        print()
        print("  The ranking is off, so every subscribed instrument is scanned by design.")
        print("  Nothing below is a fault; the comparison is only meaningful with it on.")
        return 0

    if not ranked:
        print()
        print(f"  {BAD} Nothing was ranked for {day}. The scanner correctly fails open and watches")
        print("        everything, so no filtering happened at all. Check that the refresh ran:")
        print("        docker compose logs --since 2h scanner-worker | grep universe_refresh")
        return 1

    # The failure the 12 October bug produced, and the one thing this script
    # exists to answer. Not "was the ranking right" -- it was -- but "was it
    # obeyed".
    leaked = scanned - selected
    missing = selected - scanned

    print()
    if leaked:
        print(f"  {BAD} SCANNED BUT NOT SELECTED -- the filter is not being applied:")
        print(f"        {listed(names, leaked)}")
        rejections = {entry.instrument_token: entry.rejection_reason for entry in entries}
        for token in sorted(leaked, key=lambda item: names.get(item, item)):
            reason = rejections.get(token)
            print(f"          {names.get(token, token):<12} {reason or 'not ranked at all'}")
        print()
        print("        A rejected instrument reaching a strategy means the gate let it through.")
        print("        Send this output on; it is the same shape as the 12 October failure.")
    else:
        print(f"  {OK} Nothing was scanned that the ranking had not selected.")

    if missing:
        # Not a fault: a selected instrument with no evaluation row simply never
        # produced a candle worth scoring, which a thin tape does on its own.
        print()
        print(f"  {WARN} Selected but never scanned ({len(missing)}): {listed(names, missing)}")
        print("        Usually a quiet instrument with no candles rather than a gate problem.")

    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
