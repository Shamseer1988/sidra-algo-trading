"""Did the watchlist ranking actually govern what was scanned today?

The ranking can be right and still not be applied, and no screen showed the
difference: every view reported what was ranked and selected, all of which was
correct, while the scanner evaluated instruments the ranking had refused.

Answering it means comparing the selection against what a strategy actually
scored -- and two things write evaluation rows for an instrument the universe
correctly refused, so a naive comparison cries wolf every single day:

* **The data-quality gate runs first.** ``on_completed_candle`` records a
  ``data-quality`` evaluation for a candle whose quality is stale, missing or
  invalid and returns there, never reaching the universe gate below it. Those
  rows say nothing about the universe.

* **The first candle of the day is scanned before the ranking exists.** The
  ranking is written shortly after 09:15 by design -- the gap component of its
  score needs the opening candle -- and until it is there the gate fails open,
  because a ranking that has not run must not halt trading. Every subscribed
  instrument therefore gets one minute of evaluation, legitimately.

So the comparison here is narrow on purpose: evaluations by a *strategy*, on
candles that opened at or after the ranking for that session was written. Both
exclusions are printed rather than applied silently, because an operator who
cannot see what was dropped cannot tell this script from the earlier one that
reported a leak every morning.

The candle at the boundary is excluded deliberately. An evaluation row is
written after its gate check, so a row committed in the same second as the
ranking cannot be proven to have seen it -- and a real leak is a cached empty
universe that lasts the whole session, hundreds of candles, never one.

Read-only, and safe during a session.

Usage:

    docker compose exec api python scripts/universe_applied.py
    docker compose exec api python scripts/universe_applied.py --date 2026-10-13
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.api.routes.settings import QUALITY_GATE_ID  # noqa: E402
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


def named(names: dict[str, str], tokens) -> str:
    return ", ".join(sorted(names.get(token, token) for token in tokens))


def ist(moment) -> str:
    return moment.astimezone(MARKET_TIMEZONE).strftime("%H:%M:%S")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    args = parser.parse_args()
    day = args.date or datetime.now(MARKET_TIMEZONE).date()

    from app.core.config import get_settings

    on = await ranking_is_on(get_settings())

    async with SessionLocal() as session:
        entries = list(
            (await session.scalars(select(ScanUniverseEntry).where(ScanUniverseEntry.session_date == day))).all()
        )
        # The latest build, not the first: a worker restart rebuilds the
        # ranking mid-session, and the selection now stored is the one that
        # build produced. Rows have to be judged against the ranking they were
        # actually judged by.
        written_at = max((entry.created_at for entry in entries), default=None)

        # Grouped in the database: a session is thousands of evaluations and a
        # few dozen (instrument, strategy) pairs.
        rows = list(
            (
                await session.execute(
                    select(
                        ScannerEvaluation.instrument_token,
                        ScannerEvaluation.strategy_id,
                        func.max(ScannerEvaluation.candle_opened_at),
                        func.count(),
                    )
                    .where(ScannerEvaluation.session_date == day)
                    .group_by(ScannerEvaluation.instrument_token, ScannerEvaluation.strategy_id)
                )
            ).all()
        )
        ranked = {entry.instrument_token for entry in entries}
        touched = {token for token, *_ in rows}
        names = await resolve_script_names(session, ranked | touched)

    selected = {entry.instrument_token for entry in entries if entry.selected}

    heading(f"{day} — did the ranking govern the scan?")
    print(f"  ranking enabled            : {on}")
    print(f"  ranked                     : {len(ranked)}")
    print(f"  selected by the ranking    : {len(selected)}")

    if not on:
        print()
        print("  The ranking is off, so every subscribed instrument is scanned by design.")
        print("  Nothing below would be a fault; the comparison only means something with it on.")
        return 0

    if not ranked or written_at is None:
        print()
        print(f"  {BAD} Nothing was ranked for {day}. The scanner correctly fails open and watches")
        print("        everything, so no filtering happened at all. Check that the refresh ran:")
        print("        docker compose logs --since 2h scanner-worker | grep universe_refresh")
        return 1

    # Three buckets, and only the first says anything about the gate.
    governed: set[str] = set()
    before_the_ranking: set[str] = set()
    quality_only: dict[str, int] = {}
    for token in touched:
        mine = [row for row in rows if row[0] == token]
        scored = [row for row in mine if row[1] != QUALITY_GATE_ID]
        if not scored:
            quality_only[token] = sum(row[3] for row in mine)
        elif any(row[2] >= written_at for row in scored):
            governed.add(token)
        else:
            before_the_ranking.add(token)

    print(f"  ranking written at         : {ist(written_at)} IST")
    print(f"  scanned under that ranking : {len(governed)}")

    leaked = governed - selected
    print()
    if leaked:
        print(f"  {BAD} SCANNED UNDER THIS RANKING BUT NOT SELECTED BY IT:")
        rejections = {entry.instrument_token: entry.rejection_reason for entry in entries}
        for token in sorted(leaked, key=lambda item: names.get(item, item)):
            print(f"          {names.get(token, token):<12} {rejections.get(token) or 'not ranked at all'}")
        print()
        print("        These were scored by a strategy on candles that opened after the ranking")
        print("        was written, so the gate had it and let them through. Send this output on.")
    else:
        print(f"  {OK} Nothing a strategy scored under this ranking was outside its selection.")

    excluded = before_the_ranking or quality_only or (selected - governed)
    if excluded:
        print()
        print("  Not counted above, and why:")
    if before_the_ranking:
        print(f"    {len(before_the_ranking):>3}  scored only before the ranking was written, when the gate")
        print(f"         fails open by design: {named(names, before_the_ranking)}")
    if quality_only:
        print(f"    {len(quality_only):>3}  data-quality blocks only, which are recorded before the universe")
        print(f"         gate is consulted: {named(names, quality_only)}")
    missing = selected - governed
    if missing:
        # Not a fault: a quiet instrument produces no candle worth scoring, and
        # early in the session most of the selection has not been reached yet.
        print(f"    {len(missing):>3}  selected but not yet scored under the ranking, usually a quiet")
        print(f"         instrument or an early-morning run: {named(names, missing)}")

    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
