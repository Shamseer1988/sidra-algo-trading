"""Run ORB Retest, VWAP Pullback and EMA Momentum as PAPER ONLY. Shamseer runs this.

The point is a side-by-side record: every strategy signalling on the same days
and the same shares, so their results can be compared on equal terms before any
of them is trusted with real money again. RS Pullback stays the only live
strategy; this script never touches it.

For each of the three stored entries it sets ``enabled: true`` and
``live_orders: false`` and leaves every other field as it was saved. A
paper-only strategy signals, paper trades and alerts marked PAPER ONLY, never
sends an order to the broker, and its paper fills are kept out of the live
daily ceilings -- the same treatment the CPR paper strategy already has.

Written as a settings revision and an audit-log row, like a save from the
Strategies screen.

Usage:

    docker compose exec api python scripts/apply_paper_only_comparison.py            # show what would change
    docker compose exec api python scripts/apply_paper_only_comparison.py --apply    # write it
    docker compose exec api python scripts/apply_paper_only_comparison.py --disable --apply   # switch the three off again

Running it again changes nothing if the entries are already stored like this.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ApplicationSetting, AuditLog  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.settings_history import record_revision, summarise_strategies  # noqa: E402
from app.services.strategy_registry import (  # noqa: E402
    DEFAULT_STRATEGIES,
    STRATEGIES_KEY,
    StrategyConfiguration,
)

PAPER_IDS = ("orb-retest-default", "vwap-pullback-default", "ema-momentum-default")
# The live strategy. Named so the script can prove it left it alone.
LIVE_ID = "rs-pullback-default"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the change. Without it, only show it.")
    parser.add_argument("--disable", action="store_true", help="Set the three entries to enabled=false instead.")
    args = parser.parse_args()

    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
        stored = [StrategyConfiguration.model_validate(v) for v in (setting.value if setting else DEFAULT_STRATEGIES)]
        before = [item.model_dump() for item in stored]

        missing = [sid for sid in PAPER_IDS if not any(item.id == sid for item in stored)]
        if missing:
            print(f"Refused: no stored strategy with id {', '.join(missing)}. Nothing written.")
            return 1

        updated: list[StrategyConfiguration] = []
        changed: list[str] = []
        for item in stored:
            if item.id in PAPER_IDS:
                wanted = {"enabled": False} if args.disable else {"enabled": True, "live_orders": False}
                if any(getattr(item, key) != value for key, value in wanted.items()):
                    item = item.model_copy(update={**wanted, "version": item.version + 1})
                    changed.append(item.id)
            updated.append(item)

        # The guarantees this script exists to make.
        for item in updated:
            if item.id in PAPER_IDS and item.enabled and item.live_orders:
                print(f"Refused: {item.id} would be enabled with live_orders=true. Nothing written.")
                return 1
        old_live = next((item.model_dump() for item in stored if item.id == LIVE_ID), None)
        new_live = next((item.model_dump() for item in updated if item.id == LIVE_ID), None)
        if old_live != new_live:
            print(f"Refused: {LIVE_ID} would change. Nothing written.")
            return 1

        print("Strategies after this change:")
        for item in updated:
            mode = "LIVE" if item.live_orders else "PAPER ONLY"
            mark = "  <- changed" if item.id in changed else ""
            print(f"  {item.id:<28} {item.strategy_type:<20} enabled={item.enabled!s:<5} {mode}{mark}")

        if not changed:
            print("\nAlready stored exactly like this. Nothing to do.")
            return 0
        if not args.apply:
            print("\nNothing written. Run again with --apply to store it.")
            return 0

        after = [item.model_dump() for item in updated]
        summary = summarise_strategies(before, after)
        if setting is None:
            session.add(ApplicationSetting(key=STRATEGIES_KEY, value=after))
        else:
            setting.value = after
        await record_revision(session, STRATEGIES_KEY, after, summary)
        session.add(
            AuditLog(
                event_type="settings.strategies_updated",
                metadata_json={
                    "count": len(after),
                    "changed_keys": summary.changed_keys,
                    "risk_increased": summary.risk_increased,
                    "source": "scripts/apply_paper_only_comparison.py",
                },
            )
        )
        await session.commit()
        print("\nStored. The scanner reads strategies on every candle; no restart is needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
