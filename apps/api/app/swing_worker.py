"""The swing desk's paper worker: fetch the day's candles, run the evening, report.

Runs as its own service (``swing-worker``), apart from the intraday scanner, so
nothing it does can slow or stop the scanner. It does nothing at all while:

* the swing desk is switched off in Settings (it is off by default) and no paper
  position is left to manage -- switched off, it still manages open positions
  to their exits, but cancels waiting plans and makes no new ones;
* the market is open (09:00-15:40 IST on a weekday) -- it never competes with
  the intraday desk for the database or the broker's rate limit.

Otherwise, every 15 minutes it:

1. fetches the last ten days of daily candles for the NIFTY 200 and the index,
   at most once an hour, and today's only after 18:00 IST, because a daily
   candle fetched too soon after the close may not be final -- and the backfill
   never overwrites a day it has stored;
2. processes every trading day with an index candle that it has not processed
   yet, oldest first, through ``swing_paper.run_evening`` (paper only);
3. sends that evening's summary to Telegram, marked PAPER ONLY.

It can place no order. It builds no order request, and the live risk engine
refuses every swing order regardless.
"""

import asyncio
from datetime import datetime, time, timedelta

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import ApplicationSetting, CorporateEvent, SwingPaperPosition
from app.db.session import SessionLocal
from app.services.desks import SWING_DESK, desk_enabled
from app.services.market_calculations import MARKET_TIMEZONE
from app.services.swing_backtest import SwingRules
from app.services.swing_data import backfill_daily_range, load_adjusted_daily
from app.services.swing_paper import (
    PAPER_STRATEGIES,
    EveningReport,
    days_to_process,
    last_processed,
    mark_processed,
    run_evening,
    summary_message,
)
from app.services.swing_universe import nifty200

TRADING_KEY = "trading_controls"
POLL_SECONDS = 900
FETCH_EVERY_SECONDS = 3600
HISTORY_DAYS = 460  # enough calendar days for 200 sessions of warm-up plus the relative-strength lookback
TODAY_FINAL_AFTER = time(18, 0)


def market_quiet(now: datetime) -> bool:
    """A weekday between 09:00 and 15:40 IST: the swing desk keeps out of the way."""
    return now.weekday() < 5 and time(9, 0) <= now.time() <= time(15, 40)


def last_final_day(now: datetime):  # noqa: ANN201
    """The latest day whose daily candle can be trusted as final."""
    return now.date() if now.time() >= TODAY_FINAL_AFTER else now.date() - timedelta(days=1)


async def swing_enabled() -> bool:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, TRADING_KEY)
    return desk_enabled(row.value if row and isinstance(row.value, dict) else {}, SWING_DESK)


async def positions_held() -> bool:
    """Whether a paper position is still open, or a plan is still waiting to be bought or cancelled."""
    async with SessionLocal() as session:
        found = await session.scalar(
            select(SwingPaperPosition.id).where(SwingPaperPosition.status.in_(("OPEN", "PLANNED"))).limit(1)
        )
    return found is not None


async def refresh(settings, logger, now: datetime) -> None:  # noqa: ANN001
    from app.services.upstox_oauth import load_access_token

    token = await load_access_token(settings)
    if not token:
        logger.warning("swing.refresh_skipped_no_token")
        return
    to_date = last_final_day(now)
    tokens = [settings.upstox_nifty_benchmark_key] + [member.instrument_token for member in nifty200()]
    report = await backfill_daily_range(tokens, to_date - timedelta(days=10), to_date, access_token=token)
    logger.info("swing.refreshed", stored=report.total_stored, failures=len(report.failures))


async def process(settings, now: datetime, *, new_entries: bool = True) -> list[EveningReport]:  # noqa: ANN001
    members = nifty200()
    sectors = {member.instrument_token: member.industry for member in members}
    symbols = {member.instrument_token: member.symbol for member in members}
    since = now.date() - timedelta(days=HISTORY_DAYS)
    final = last_final_day(now)
    async with SessionLocal() as session:
        benchmark = [
            bar
            for bar in await load_adjusted_daily(session, settings.upstox_nifty_benchmark_key, from_date=since)
            if bar.session_date <= final
        ]
        days = days_to_process([bar.session_date for bar in benchmark], await last_processed(session))
        if not days:
            return []
        universe = {
            member.instrument_token: await load_adjusted_daily(session, member.instrument_token, from_date=since)
            for member in members
        }
        events = list(
            (
                await session.scalars(
                    select(CorporateEvent).where(CorporateEvent.ex_date >= since, CorporateEvent.confirmed.is_(True))
                )
            ).all()
        )
    reports = []
    for day in days:
        # Each evening sees only what was known that evening.
        seen = {token: [bar for bar in bars if bar.session_date <= day] for token, bars in universe.items()}
        index = [bar for bar in benchmark if bar.session_date <= day]
        async with SessionLocal() as session:
            report = await run_evening(
                session,
                day,
                universe=seen,
                benchmark=index,
                sectors=sectors,
                symbols=symbols,
                events=events,
                new_entries=new_entries,
                rules=SwingRules(strategies=PAPER_STRATEGIES),
            )
            await mark_processed(session, day)
            await session.commit()
        reports.append(report)
    return reports


async def announce(settings, report: EveningReport, logger) -> None:  # noqa: ANN001
    from app.services.telegram import TelegramNotificationService

    if not settings.telegram_chat_id:
        return
    try:
        await TelegramNotificationService(settings).send_message(summary_message(report), parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - a missed message never stops the desk
        logger.warning("swing.telegram_failed", error=f"{type(exc).__name__}: {exc}")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger("swing")
    logger.info("swing.worker_started", paper_only=True)
    last_fetch = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            now = datetime.now(MARKET_TIMEZONE)
            enabled = await swing_enabled()
            # Off stops new plans, never the positions already held: those are
            # still managed to their exits, and waiting plans are cancelled.
            if not market_quiet(now) and (enabled or await positions_held()):
                if loop.time() - last_fetch >= FETCH_EVERY_SECONDS:
                    await refresh(settings, logger, now)
                    last_fetch = loop.time()
                for report in await process(settings, now, new_entries=enabled):
                    logger.info(
                        "swing.evening_processed",
                        day=report.day.isoformat(),
                        filled=len(report.filled),
                        closed=len(report.closed),
                        planned=len(report.planned),
                        regime_ok=report.regime_ok,
                    )
                    await announce(settings, report, logger)
        except Exception:
            logger.exception("swing.worker_cycle_failed")
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
