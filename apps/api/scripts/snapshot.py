"""One command that says what this deployment is, how it is configured, and what it did.

Written so a question about the running system can be answered from one paste
instead of five. Everything here is read-only: it opens the database, reads
Redis, imports modules to see which version of the code is in the image, and
asks the broker nothing.

**No secret is ever printed.** Credentials appear as "configured: yes" and never
as a value — not masked, not truncated, not the first four characters. A
diagnostic whose output gets pasted into a chat window is the last place a token
should be able to appear.

Usage:

    docker compose exec api python scripts/snapshot.py
    docker compose exec api python scripts/snapshot.py --date 2026-10-08
"""

import argparse
import asyncio
import importlib
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    ApplicationSetting,
    ExecutionReconciliation,
    InstrumentMasterRefresh,
    LiveOrderSubmission,
    PaperSignal,
    ScanUniverseEntry,
)
from app.db.session import SessionLocal  # noqa: E402
from app.services.trade_counter import session_bounds_utc  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78
OK, WARN, BAD = "[OK]  ", "[WARN]", "[BAD] "

# Which fixes are in this image. Each is a symbol that did not exist before the
# change that introduced it, so importing the module answers "did that deploy
# actually reach the container" without needing git inside it — and a container
# running last week's code is the explanation that gets reached for last.
FEATURES = [
    ("per-instrument tick size", "app.services.trading_symbols", "instrument_tick_size"),
    ("entry cap measured in R", "app.services.entry_pricing", "DEFAULT_CAP_R"),
    ("share-price band on every signal", "app.services.risk_engine", "outside_price_band"),
    ("trade-closed alerts", "app.services.live_trade_alerts", "closed_trades"),
    ("break-even trail, live", "app.services.live_exit_manager", "_trail_stop"),
    ("stale-price guard on exits", "app.services.live_exit_manager", "price_refusal"),
    ("unattended order recovery", "app.services.live_order_recovery", "pending_resolution"),
    ("broker settlement catch-up", "app.services.broker_day_figures", "sync_days"),
    ("execution quality report", "app.services.execution_quality", "build_report"),
]


def heading(title: str) -> None:
    print()
    print(RULE)
    print(title)
    print(RULE)


def yes(value: object) -> str:
    return "yes" if value else "no"


def ist(value: datetime | None) -> str:
    return value.astimezone(MARKET_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S IST") if value else "never"


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def main(args) -> int:
    settings = get_settings()
    session_date = args.date or datetime.now(MARKET_TIMEZONE).date()
    problems = 0

    heading(f"SIDRA SNAPSHOT   {datetime.now(MARKET_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"  python                 : {sys.version.split()[0]}")

    heading("1. What code is in this image")
    for label, module_name, symbol in FEATURES:
        try:
            present = hasattr(importlib.import_module(module_name), symbol)
        except ModuleNotFoundError:
            present = False
        print(f"  {OK if present else BAD} {label}")
        if not present:
            problems += 1
    if problems:
        print()
        print("  Something above is missing: this container is running older code than the")
        print("  repository. Rebuild with --no-cache and restart before reading anything else.")

    heading("2. Runtime")
    print(f"  application_mode       : {settings.application_mode}")
    print(f"  live_trading_enabled   : {yes(settings.live_trading_enabled)}")
    print(f"  live_shadow_enabled    : {yes(settings.live_shadow_enabled)}")
    print(f"  universe_enabled       : {yes(settings.universe_enabled)}")
    print()
    print("  credentials (presence only; no value is ever printed)")
    print(f"    upstox               : {yes(settings.upstox_is_configured)}")
    print(f"    upstox oauth         : {yes(settings.upstox_oauth_is_configured)}")
    print(f"    upstox auto-auth     : {yes(settings.upstox_auto_auth_is_configured)}")
    print(f"    firstock             : {yes(settings.firstock_is_configured)}")
    print(f"    telegram             : {yes(settings.telegram_is_configured)}")
    if settings.application_mode == "LIVE" and settings.live_shadow_enabled:
        print(f"  {WARN} Shadow is rehearsal machinery. Alongside live trading it is pure overhead.")

    async with SessionLocal() as session:
        heading("3. Database")
        version = await session.scalar(text("select version_num from alembic_version"))
        print(f"  alembic head           : {version}")
        counts = {
            "paper_signals": PaperSignal,
            "live_order_submissions": LiveOrderSubmission,
        }
        for name, model in counts.items():
            total = await session.scalar(select(func.count(model.id)))
            print(f"  {name:22} : {total:,} rows")

        heading("4. Trading controls")
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS)
        if row is None:
            print(f"  {WARN} Nothing saved; these are the built-in defaults.")
        for field in sorted(controls.model_dump()):
            print(f"  {field:30} : {controls.model_dump()[field]}")

        heading("5. Strategies")
        from app.services.strategy_registry import StrategyRegistry

        strategies = await StrategyRegistry.enabled(session)
        if not strategies:
            print(f"  {BAD} No strategy is enabled. Nothing can be traded.")
            problems += 1
        for item in strategies:
            rules = item.exit_rules
            print()
            print(f"  {item.name}  ({item.strategy_type} v{item.version})")
            print(
                f"    sides {','.join(item.allowed_sides)}   min score {item.minimum_score}   "
                f"min R:R {item.minimum_rr}   max trades/day {item.max_trades_per_day}"
            )
            print(f"    universe            : {len(item.universe) or 'every instrument scanned'}")
            print(
                f"    stop                : {rules.stop_rule} (atr x{rules.stop_atr_multiple or 'account'}, "
                f"floor {rules.min_stop_distance_percent or 'account'}%)"
            )
            print(f"    target              : {rules.target_rule} rr={rules.target_rr or 'strategy minimum'}")
            print(f"    trailing            : {rules.trailing_rule} at {rules.trailing_trigger_r}R")
            print(
                f"    square-off          : {rules.square_off_time or 'none of its own'}   "
                f"time exit {rules.time_exit_minutes or 'none'}"
            )
            if rules.trailing_rule == "ATR_TRAIL":
                print(f"    {WARN} ATR trailing is paper-only; live trades keep the stop they were given.")

        heading("6. Instrument master")
        master = await session.scalar(
            select(InstrumentMasterRefresh).order_by(InstrumentMasterRefresh.fetched_at.desc()).limit(1)
        )
        if master is None:
            print(f"  {BAD} Never fetched. Every order falls back to the default tick grid.")
            problems += 1
        else:
            entries = master.configured_keys or {}
            with_tick = {
                k: v.get("tick_size") for k, v in entries.items() if isinstance(v, dict) and v.get("tick_size")
            }
            print(f"  fetched                : {ist(master.fetched_at)}")
            print(f"  instruments in file    : {master.instrument_count:,}")
            print(f"  subscribed + stored    : {len(entries)}")
            print(f"  with a tick size       : {len(with_tick)}")
            if master.missing_keys:
                print(f"  {WARN} not found in the master: {', '.join(master.missing_keys[:5])}")
            if not with_tick:
                problems += 1
                print(f"  {BAD} No tick sizes stored. The master predates the per-instrument tick fix;")
                print("        restart the scanner so it refreshes, or orders stay on the fallback grid.")
            else:
                grids: dict[str, int] = {}
                for value in with_tick.values():
                    grids[str(Decimal(str(value)) / 100)] = grids.get(str(Decimal(str(value)) / 100), 0) + 1
                print("  grids in use           : " + ", ".join(f"Rs{k} x{v}" for k, v in sorted(grids.items())))

        heading(f"7. The session {session_date}")
        start, end = session_bounds_utc(session_date)
        signals = await session.scalar(
            select(func.count(PaperSignal.id)).where(PaperSignal.session_date == session_date)
        )
        print(f"  signals recorded       : {signals}")
        rows = await session.execute(
            select(LiveOrderSubmission.status, func.count(LiveOrderSubmission.id))
            .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
            .group_by(LiveOrderSubmission.status)
        )
        live = dict(rows.all())
        print(
            f"  live submissions       : {sum(live.values()) or 'none'}"
            + (f"   ({', '.join(f'{k} {v}' for k, v in sorted(live.items()))})" if live else "")
        )
        universe = await session.scalar(
            select(func.count(ScanUniverseEntry.instrument_token)).where(
                ScanUniverseEntry.session_date == session_date, ScanUniverseEntry.selected.is_(True)
            )
        )
        print(f"  universe selected      : {universe or 'not built (every instrument scanned)'}")

        heading("8. Health")
        stuck = await session.scalar(
            select(func.count(LiveOrderSubmission.id)).where(
                LiveOrderSubmission.status.in_(["UNKNOWN", "NEEDS_REVIEW", "PREPARED"])
            )
        )
        if stuck:
            problems += 1
            print(f"  {BAD} {stuck} submission(s) have no settled outcome. Live trading is blocked until they do.")
        else:
            print(f"  {OK} Every submission has a settled outcome.")

        latest = await session.scalar(
            select(ExecutionReconciliation)
            .where(ExecutionReconciliation.mode == "LIVE")
            .order_by(ExecutionReconciliation.created_at.desc())
            .limit(1)
        )
        if latest is None:
            print(f"  {WARN} No live reconciliation has ever run; arming will be refused.")
        else:
            stamped = latest.created_at if latest.created_at.tzinfo else latest.created_at.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - stamped).total_seconds()
            # Freshness only matters while the exchange is open: the refresh job
            # runs 09:00-15:59, so every evening reading is "stale" and warning
            # about it teaches an operator to ignore the line.
            from app.services.trading_calendar import MarketPhase, TradingCalendar

            status = TradingCalendar.from_settings(settings).status_at(datetime.now(UTC))
            trading_now = status.trading_day and status.phase in {MarketPhase.OPEN, MarketPhase.PRE_OPEN}
            mark = OK if latest.safe_to_trade and (age < 900 or not trading_now) else WARN
            print(
                f"  {mark} Last live reconciliation {ist(latest.created_at)} "
                f"({age / 60:.0f} min ago), safe_to_trade={yes(latest.safe_to_trade)}"
                + ("" if trading_now else "   [exchange closed; it refreshes 09:00-15:59]")
            )

    try:
        from redis.asyncio import Redis

        redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
        tracking = await redis.get("safety:paper_tracking_enabled")
        stop = await redis.hgetall("safety:emergency_stop")
        state = await redis.get("scanner:worker_state")
        print(f"  {OK} Redis reachable.")
        print(f"  paper tracking         : {'off' if tracking == 'false' else 'on'}")
        print(f"  emergency stop         : {'ACTIVE' if stop.get('active') == 'true' else 'clear'}")
        print(f"  scanner worker state   : {state or 'unknown'}")
        if tracking == "false":
            problems += 1
            print(f"  {BAD} Paper tracking is off. It is the scanner's master switch: no signals, no live orders.")
        if stop.get("active") == "true":
            problems += 1
            print(f"  {BAD} Emergency stop is active. Nothing will be traded until it is cleared.")
        await redis.aclose()
    except Exception as error:  # noqa: BLE001 - reporting tool
        problems += 1
        print(f"  {BAD} Redis unreachable: {type(error).__name__}: {error}")

    heading("VERDICT")
    if problems:
        print(f"  {problems} thing(s) above need attention before this is left unattended.")
    else:
        print(f"  {OK} Nothing here is blocking. Watch the live day with scripts/inspect_live_day.py.")
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=parse_day, help="Session to summarise (YYYY-MM-DD). Default: today in IST.")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
