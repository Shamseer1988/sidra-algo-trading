"""Empty the trading history, keeping the configuration that produced it.

Starting a fresh record is an ordinary thing to want after a rebuild, and doing
it with a hand-written DELETE is how a strategy definition or a broker token
gets taken out along with the trades. So the two sets are written down here, in
full, and the script refuses to run unless they account for every table in the
database.

**That refusal is the point.** A table added in a later migration belongs to
neither list, and the safe response to "I do not know what this is" is to stop,
not to guess. Guessing in one direction silently keeps history the operator
asked to clear; guessing in the other silently destroys something. Either way
they find out later, from the consequences.

Deletion is not recoverable. The script prints the backup command and makes the
operator type a full sentence, because a y/n prompt is answered reflexively.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402

CONFIRMATION = "DELETE ALL HISTORY"

# Configuration and identity. Nothing here is a record of what the system did;
# it is what the system is. broker_credentials holds the encrypted Upstox token
# and application_settings holds the strategies, the trading controls and the
# indicator periods — losing either means reconfiguring from scratch.
KEEP = {
    "users",
    "user_sessions",
    "application_settings",
    "setting_revisions",
    "broker_credentials",
    "instrument_master_refreshes",
    "alembic_version",
}

# Everything the system recorded about its own operation.
CLEAR = {
    # what the scanner saw and decided
    "market_candles",
    "market_indicator_snapshots",
    "scanner_evaluations",
    "scan_universe",
    "paper_signals",
    "paper_signal_outcomes",
    # the paper account
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "risk_reservations",
    "session_halts",
    # the live path, including everything it only rehearsed
    "order_intents",
    "oms_orders",
    "oms_order_events",
    "execution_reconciliations",
    "shadow_orders",
    "live_shadow_decisions",
    "live_activations",
    "live_order_submissions",
    "live_order_approvals",
    "live_readiness_checks",
    "trade_approval_intents",
    "broker_day_snapshots",
    # research
    "backtest_runs",
    "backtest_trades",
    "backtest_sweeps",
    # messages and access
    "telegram_alerts",
    "telegram_inbound_events",
    "audit_logs",
    "login_history",
}


async def tables_in_database(session) -> set[str]:
    rows = await session.execute(text("select tablename from pg_tables where schemaname = current_schema()"))
    return {row[0] for row in rows}


async def cascade_would_reach_kept(session) -> list[tuple[str, str]]:
    """Find kept tables that a CASCADE on a cleared table would empty too.

    TRUNCATE ... CASCADE follows foreign keys *inward*: truncating a table also
    truncates everything that references it. A kept table referencing a cleared
    one would therefore be emptied without ever appearing in CLEAR, which is the
    failure this whole file exists to prevent.
    """
    rows = await session.execute(
        text("""
            select child.relname, parent.relname
              from pg_constraint c
              join pg_class child on child.oid = c.conrelid
              join pg_class parent on parent.oid = c.confrelid
             where c.contype = 'f'
        """)
    )
    return [(child, parent) for child, parent in rows if child in KEEP and parent in CLEAR]


async def counts(session, tables: set[str]) -> dict[str, int]:
    found = {}
    for table in sorted(tables):
        total = await session.scalar(text(f'select count(*) from "{table}"'))  # noqa: S608 - names are from this file
        found[table] = int(total or 0)
    return found


async def main(assume_yes: bool) -> int:
    async with SessionLocal() as session:
        present = await tables_in_database(session)
        unknown = present - KEEP - CLEAR
        if unknown:
            print("STOPPED. These tables are in the database but in neither list:")
            for name in sorted(unknown):
                print(f"  {name}")
            print()
            print("A migration has added something this script does not know about.")
            print("Decide where each belongs and add it to KEEP or CLEAR, then re-run.")
            print("Nothing was deleted.")
            return 2

        collisions = await cascade_would_reach_kept(session)
        if collisions:
            print("STOPPED. Truncating these would cascade into tables meant to be kept:")
            for child, parent in collisions:
                print(f"  {child} references {parent}")
            print("Nothing was deleted.")
            return 2

        targets = sorted(CLEAR & present)
        before = await counts(session, set(targets))
        total = sum(before.values())

        print("=" * 70)
        print("KEEPING — configuration, strategies, credentials, identity")
        print("=" * 70)
        kept = await counts(session, KEEP & present)
        for name, rows in kept.items():
            print(f"  {name:32} {rows:>10,} rows")

        print()
        print("=" * 70)
        print("CLEARING — every record of what the system did")
        print("=" * 70)
        for name in targets:
            if before[name]:
                print(f"  {name:32} {before[name]:>10,} rows")
        empty = [name for name in targets if not before[name]]
        if empty:
            print(f"  ({len(empty)} further tables are already empty)")

        print()
        print(f"  {total:,} rows will be deleted. This cannot be undone.")
        print()
        print("  Back up first if you have not:")
        print("    docker compose exec postgres pg_dump -U intraday_sentinel \\")
        print("      intraday_sentinel > backups/before-purge-$(date +%F).sql")
        print()

        if not total:
            print("  Nothing to delete.")
            return 0

        if not assume_yes:
            print(f'  Type exactly "{CONFIRMATION}" to continue, or anything else to stop.')
            if input("  > ").strip() != CONFIRMATION:
                print("  Stopped. Nothing was deleted.")
                return 1

        # One statement, one transaction: a purge that half-succeeded would
        # leave orders without their signals, which is worse than either
        # outcome it sits between.
        quoted = ", ".join(f'"{name}"' for name in targets)
        await session.execute(text(f"truncate table {quoted} restart identity cascade"))  # noqa: S608
        await session.commit()

        after = await counts(session, set(targets))
        remaining = {name: rows for name, rows in after.items() if rows}
        print()
        if remaining:
            print("  [PROBLEM] These still hold rows:")
            for name, rows in remaining.items():
                print(f"    {name}: {rows}")
            return 1

        still_kept = await counts(session, KEEP & present)
        lost = [name for name, rows in kept.items() if rows and not still_kept.get(name)]
        if lost:
            print("  [PROBLEM] These were meant to be kept and are now empty:")
            for name in lost:
                print(f"    {name}")
            return 1

        print(f"  Done. {total:,} rows deleted, configuration intact.")
        print("  Restart the scanner to begin a fresh record.")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="Skip the typed confirmation. For scripted use only.")
    raise SystemExit(asyncio.run(main(parser.parse_args().yes)))
