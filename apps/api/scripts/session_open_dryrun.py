"""Rehearse tomorrow's 08:45 open without arming anything.

Runs the real ``open_live_session`` with ``dry_run=True``: the same calendar
check, the same runtime guard, the same broker login and the same reconcile the
scheduled job will run, stopping immediately before the arm.

Nothing here places, modifies or cancels an order, and nothing is armed. Safe
to run during market hours.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.live_session_open import open_live_session  # noqa: E402
from app.services.trading_calendar import TradingCalendar  # noqa: E402


async def main() -> int:
    settings = get_settings()
    calendar = TradingCalendar.from_settings(settings)

    print("=" * 70)
    print("SCHEDULED OPEN — DRY RUN (arms nothing, submits nothing)")
    print("=" * 70)
    print(f"  APPLICATION_MODE     : {settings.application_mode}")
    print(f"  LIVE_TRADING_ENABLED : {settings.live_trading_enabled}")
    print()

    result = await open_live_session(settings, calendar, dry_run=True)

    print(f"  Stopped at : {result.step}")
    print(f"  Detail     : {result.detail}")
    for item in result.findings:
        if item:
            print(f"    - {item}")
    print()

    if result.step == "dry_run":
        print("  [OK] Every check the scheduled job makes before arming passed.")
        print("       At 08:45 on a trading day it would arm and start the scanner.")
        return 0
    if result.step == "already_armed":
        print("  [OK] Already armed; the job would leave the existing window alone.")
        return 0
    print("  [BLOCKED] The scheduled job would NOT arm. Resolve the above first.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
