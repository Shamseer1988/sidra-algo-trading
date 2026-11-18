"""Add the CPR First-Hour Breakout strategy as PAPER ONLY. Shamseer runs this.

Writes one entry into the stored strategies (``paper_strategies``), recorded as
a settings revision and an audit-log row like a save from the Strategies
screen. Every other stored strategy is left exactly as it is.

The entry is enabled with ``live_orders: false``: it signals, paper trades and
sends Telegram alerts marked PAPER ONLY, and it never sends an order to the
broker. Its paper fills are kept out of the live strategy's daily ceilings.

Rules (G Stock Academy notes and the money-management sheet): buy and sell,
10:15 AM - 2:45 PM, stop 0.5% below entry, target 1:2, up to 3 paper trades a
day. minimum_score 0 because the CPR rules themselves are the filter.

Usage:

    docker compose exec api python scripts/apply_cpr_paper_strategy.py           # show what would change
    docker compose exec api python scripts/apply_cpr_paper_strategy.py --apply   # write it

Running it again changes nothing if the entry is already there. To switch it
off later, use --disable.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ApplicationSetting, AuditLog  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.extra_strategies import CPR_BREAKOUT_VERSION  # noqa: E402
from app.services.exit_rules import ExitRules  # noqa: E402
from app.services.settings_history import record_revision, summarise_strategies  # noqa: E402
from app.services.strategy_registry import (  # noqa: E402
    DEFAULT_STRATEGIES,
    STRATEGIES_KEY,
    StrategyConfiguration,
)

CPR_ID = "cpr-breakout-paper"


def cpr_configuration() -> StrategyConfiguration:
    return StrategyConfiguration(
        id=CPR_ID,
        name="CPR First-Hour Breakout (paper)",
        enabled=True,
        live_orders=False,
        strategy_type=CPR_BREAKOUT_VERSION,
        allowed_sides=["LONG", "SHORT"],
        max_trades_per_day=3,
        minimum_score=0,
        minimum_rr=2.0,
        exit_rules=ExitRules(
            # The sheet's 0.5% stop. The ATR multiple is set tiny so the
            # account's 2.1x ATR floor cannot widen it; the percent floor is
            # the 0.5% itself.
            stop_atr_multiple=0.01,
            min_stop_distance_percent=0.5,
            target_rr=2.0,
        ),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the change. Without it, only show it.")
    parser.add_argument("--disable", action="store_true", help="Set the CPR entry to enabled=false instead.")
    args = parser.parse_args()

    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
        stored = [StrategyConfiguration.model_validate(v) for v in (setting.value if setting else DEFAULT_STRATEGIES)]
        before = [item.model_dump() for item in stored]
        existing = next((item for item in stored if item.id == CPR_ID), None)

        if args.disable:
            if existing is None:
                print("No CPR entry is stored; nothing to disable.")
                return 0
            updated = existing.model_copy(update={"enabled": False, "version": existing.version + 1})
            stored = [updated if item.id == CPR_ID else item for item in stored]
        elif existing is None:
            stored.append(cpr_configuration())
        else:
            wanted = cpr_configuration().model_dump(exclude={"version"})
            if existing.model_dump(exclude={"version"}) == wanted:
                print("The CPR paper strategy is already stored exactly like this. Nothing to do.")
                return 0
            updated = cpr_configuration().model_copy(update={"version": existing.version + 1})
            stored = [updated if item.id == CPR_ID else item for item in stored]

        # The one guarantee this script exists to make.
        cpr = next(item for item in stored if item.id == CPR_ID)
        if cpr.live_orders:
            print("Refused: the CPR entry would have live_orders=true. This script only ever writes paper-only.")
            return 1

        after = [item.model_dump() for item in stored]
        summary = summarise_strategies(before, after)
        print("Strategies after this change:")
        for item in stored:
            mode = "LIVE" if item.live_orders else "PAPER ONLY"
            print(f"  {item.id:<28} {item.strategy_type:<18} enabled={item.enabled!s:<5} {mode}")
        print("\nCPR entry:")
        print(json.dumps(cpr.model_dump(), indent=2, default=str))

        if not args.apply:
            print("\nNothing written. Run again with --apply to store it.")
            return 0

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
                    "source": "scripts/apply_cpr_paper_strategy.py",
                },
            )
        )
        await session.commit()
        print("\nStored. The scanner reads strategies on every candle; no restart is needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
