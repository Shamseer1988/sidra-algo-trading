"""APScheduler-powered morning Upstox token renewal.

Schedules a CronTrigger job at **08:30 AM IST, Monday–Friday** that:
  1. Checks the NSE TradingCalendar to skip exchange holidays.
  2. Runs the headless auto-login flow (``perform_auto_login``).
  3. Logs the result to the ``audit_logs`` table.
  4. Sends a Telegram notification on success or failure.

The scheduler is attached to the FastAPI lifespan so it starts with the
API container and shuts down cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.services.trading_calendar import MARKET_TIMEZONE, TradingCalendar
from app.services.upstox_auto_auth import UpstoxAutoAuthError, perform_auto_login

logger = structlog.get_logger("scheduler")

# Redis key that surfaces the last auto-auth result to the status API
AUTO_AUTH_STATUS_KEY = "upstox:auto_auth:last_result"


async def _persist_audit(event_type: str, metadata: dict) -> None:
    """Write an audit-log row for the auto-auth attempt."""
    try:
        async with SessionLocal() as session:
            session.add(
                AuditLog(
                    user_id=None,
                    event_type=event_type,
                    metadata_json=metadata,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("scheduler.audit_write_failed")


async def send_auto_auth_telegram_alert(
    settings: Settings,
    trigger: str,
    success: bool,
    expires_at: str | datetime | None = None,
    error: str | None = None,
) -> None:
    """Send a rich Telegram alert with detailed status of Upstox session renewal."""
    try:
        from zoneinfo import ZoneInfo

        from app.services.telegram import TelegramNotificationService
        from app.services.telegram_config import configured_settings

        effective_settings = await configured_settings(settings)
        if not effective_settings.telegram_is_configured:
            logger.debug("scheduler.telegram_skipped_not_configured")
            return

        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(UTC).astimezone(ist).strftime("%d-%b-%Y %I:%M:%S %p")

        mobile = settings.upstox_mobile_number
        masked_mobile = f"{mobile[:3]}****{mobile[-3:]}" if mobile and len(mobile) >= 6 else "N/A"

        if success:
            exp_str = "Unknown"
            if expires_at:
                if isinstance(expires_at, str):
                    exp_dt = datetime.fromisoformat(expires_at).astimezone(ist)
                else:
                    exp_dt = expires_at.astimezone(ist)
                exp_str = exp_dt.strftime("%d-%b-%Y %I:%M:%S %p")

            text = (
                "🔔 <b>UPSTOX SESSION RENEWAL</b>\n\n"
                "✅ <b>Status:</b> Success\n"
                f"⚙️ <b>Trigger:</b> {trigger}\n"
                f"⏰ <b>Renewed At:</b> {now_ist} IST\n"
                f"📅 <b>Token Expires:</b> {exp_str} IST\n"
                f"📱 <b>Account:</b> {masked_mobile}\n"
                "🔐 <b>Auth:</b> TOTP + 2FA PIN (Automated)\n\n"
                "🟢 <i>Market scanner feed is ready for the session.</i>"
            )
        else:
            text = (
                "🚨 <b>UPSTOX SESSION RENEWAL FAILED</b>\n\n"
                "❌ <b>Status:</b> Failed\n"
                f"⚙️ <b>Trigger:</b> {trigger}\n"
                f"⏰ <b>Attempted At:</b> {now_ist} IST\n"
                f"📱 <b>Account:</b> {masked_mobile}\n"
                f"⚠️ <b>Error:</b> <code>{error or 'Unknown error'}</code>\n\n"
                "👉 <i>Please renew access manually from the Settings panel.</i>"
            )

        tg = TelegramNotificationService(effective_settings)
        await tg.send_message(text, parse_mode="HTML")
        logger.info("scheduler.telegram_alert_sent", trigger=trigger, success=success)
    except Exception as exc:
        logger.warning("scheduler.telegram_alert_failed", error=str(exc))


async def run_upstox_auto_renewal(
    settings: Settings, calendar: TradingCalendar, trigger: str = "Scheduled (08:30 AM IST)"
) -> dict | None:
    """Execute the morning token renewal if today is a trading day.

    Returns the renewal metadata dict on success, or None if skipped/failed.
    """
    now_utc = datetime.now(UTC)
    market_status = calendar.status_at(now_utc)

    if not market_status.trading_day:
        logger.info(
            "scheduler.auto_renewal_skipped",
            reason=market_status.reason,
            date=now_utc.date().isoformat(),
        )
        return None

    logger.info("scheduler.auto_renewal_starting", trigger=trigger, date=now_utc.date().isoformat())

    try:
        result = await perform_auto_login(settings)
        await _persist_audit(
            "scheduler.upstox_auto_auth_success",
            {
                "expires_at": result["expires_at"],
                "renewed_at": result["renewed_at"],
                "trigger": trigger,
            },
        )
        await send_auto_auth_telegram_alert(
            settings=settings,
            trigger=trigger,
            success=True,
            expires_at=result["expires_at"],
        )
        logger.info("scheduler.auto_renewal_completed", expires_at=result["expires_at"])
        return result
    except UpstoxAutoAuthError as exc:
        error_msg = str(exc)
        await _persist_audit("scheduler.upstox_auto_auth_failed", {"error": error_msg, "trigger": trigger})
        await send_auto_auth_telegram_alert(
            settings=settings,
            trigger=trigger,
            success=False,
            error=error_msg,
        )
        logger.error("scheduler.auto_renewal_failed", error=error_msg)
        return None
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        await _persist_audit("scheduler.upstox_auto_auth_error", {"error": error_msg, "trigger": trigger})
        await send_auto_auth_telegram_alert(
            settings=settings,
            trigger=trigger,
            success=False,
            error=error_msg,
        )
        logger.exception("scheduler.auto_renewal_unexpected_error")
        return None


async def _publish_status_to_redis(settings: Settings, status_data: dict) -> None:
    """Store last auto-auth result in Redis for the status API."""
    try:
        from redis.asyncio import Redis

        redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
        try:
            import json

            await redis.set(AUTO_AUTH_STATUS_KEY, json.dumps(status_data), ex=86400)
        finally:
            await redis.aclose()
    except Exception:
        logger.warning("scheduler.redis_status_publish_failed")


def _make_job_func(settings: Settings, calendar: TradingCalendar):
    """Return the coroutine that APScheduler will call on each trigger."""

    async def _job() -> None:
        result = await run_upstox_auto_renewal(settings, calendar)
        status_data = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "success": result is not None,
            "expires_at": result["expires_at"] if result else None,
            "error": None if result else "See audit logs for details",
        }
        await _publish_status_to_redis(settings, status_data)

    return _job


def _make_broker_figures_job(settings: Settings):
    """Fetch the broker's own figures for the session that just ended.

    Skips itself on a day this system placed no live orders, which is what
    makes it harmless on a paper-only deployment: there is nothing at the
    broker to reconcile against, and asking would only produce an empty report
    and an audit entry nobody needs.

    Read-only at the broker. The client it uses has no method that can place,
    modify or cancel an order.
    """

    async def _job() -> None:
        from sqlalchemy import select

        from app.db.models import LiveOrderSubmission
        from app.db.session import SessionLocal
        from app.services import broker_day_figures, live_fills
        from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc
        from app.services.upstox_oauth import load_access_token
        from app.services.upstox_orders import UpstoxError, UpstoxReportClient, UpstoxSession

        session_date = datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()
        start, end = session_bounds_utc(session_date)
        async with SessionLocal() as db:
            placed = await db.scalar(
                select(LiveOrderSubmission.id)
                .where(
                    LiveOrderSubmission.created_at >= start,
                    LiveOrderSubmission.created_at < end,
                    LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
                )
                .limit(1)
            )
        if placed is None:
            logger.info("scheduler.broker_figures_skipped", reason="no live orders today", date=str(session_date))
            return

        token = await load_access_token(settings)
        if not token:
            logger.warning("scheduler.broker_figures_skipped", reason="no upstox access token")
            await _persist_audit("scheduler.broker_figures_skipped", {"reason": "no_access_token"})
            return

        # Before the figures, the fills. The recorder inside reconciliation only
        # runs while the deployment is armed and the exchange is open, so an
        # activation that lapsed in the afternoon would leave that afternoon's
        # trades priced by the simulator for good. This catches them once, after
        # the close, while the order book still holds the day.
        try:
            async with SessionLocal() as db:
                recorded = await live_fills.sweep_session_fills(settings, db, session_date)
            if recorded:
                logger.info("scheduler.fills_swept", date=str(session_date), submissions=recorded)
        except Exception as error:  # noqa: BLE001 - the figures fetch is the job; this is a bonus
            logger.warning("scheduler.fills_sweep_failed", error=str(error))

        client = UpstoxReportClient(settings, UpstoxSession(access_token=token))
        try:
            figures = await broker_day_figures.fetch_upstox_day(client, session_date)
        except UpstoxError as error:
            logger.warning("scheduler.broker_figures_failed", error=str(error))
            await _persist_audit("scheduler.broker_figures_failed", {"error": str(error)})
            return

        async with SessionLocal() as db:
            await broker_day_figures.record(db, session_date, figures)
            await db.commit()
        logger.info(
            "scheduler.broker_figures_recorded",
            date=str(session_date),
            trades=figures.trade_count,
            realized=str(figures.realized_pnl),
        )

    return _job


async def send_session_open_alert(settings: Settings, result) -> None:
    """Tell the operator what the scheduled open did, especially when it did nothing.

    A refusal is the message that matters most: an unattended system that
    declined to arm looks exactly like one that armed fine, right up until the
    day ends with no trades and nobody knows why.
    """
    try:
        from app.services.telegram import TelegramNotificationService
        from app.services.telegram_config import configured_settings

        effective = await configured_settings(settings)
        if not effective.telegram_is_configured:
            return

        ist = datetime.now(UTC).astimezone(MARKET_TIMEZONE).strftime("%d-%b-%Y %I:%M %p")
        if result.opened:
            # Optional only when the session actually opened. A session that did
            # NOT open is the morning's most important message -- it means
            # nothing will trade today -- and the branch below is never muted.
            from app.db.session import SessionLocal
            from app.services.notification_settings import wants

            async with SessionLocal() as session:
                if not await wants(session, "session_open_alerts"):
                    return
            expiry = (
                result.expires_at.astimezone(MARKET_TIMEZONE).strftime("%I:%M %p") if result.expires_at else "unknown"
            )
            text = (
                "\u2705 <b>Live session open</b>\n\n"
                f"\U0001f550 {ist} IST\n"
                f"\U0001f4dd {result.detail}\n"
                f"\u23f3 Activation expires <b>{expiry} IST</b>\n\n"
                "<i>Real orders can now be placed. Disarm at any time to stop.</i>"
            )
        else:
            findings = "".join(f"\n  \u2022 {item}" for item in result.findings[:5] if item)
            text = (
                "\u26d4 <b>Live session NOT opened</b>\n\n"
                f"\U0001f550 {ist} IST\n"
                f"\U0001f6d1 Stopped at: <b>{result.step}</b>\n"
                f"\U0001f4dd {result.detail}{findings}\n\n"
                "<i>Nothing is armed and the scanner was not started. "
                "No orders can be placed until this is resolved.</i>"
            )
        await TelegramNotificationService(effective).send_message(text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("scheduler.session_open_alert_failed", error=str(exc))


def _make_session_open_job(settings: Settings, calendar: TradingCalendar):
    """Reconcile, arm and start the scanner for today, or refuse and report.

    Scheduled after the 08:30 token renewal rather than alongside it: the
    reconcile needs a broker session, and two jobs racing for one would make
    the failure mode depend on which finished first.
    """

    async def _job() -> None:
        from app.services.live_session_open import open_live_session

        try:
            result = await open_live_session(settings, calendar)
        except Exception as exc:
            logger.exception("scheduler.session_open_unexpected_error")
            await _persist_audit("scheduler.live_session_open_error", {"error": f"{type(exc).__name__}: {exc}"})
            from app.services.live_session_open import OpenResult

            result = OpenResult(False, "unexpected", f"{type(exc).__name__}: {exc}")
        else:
            await _persist_audit(
                "scheduler.live_session_opened" if result.opened else "scheduler.live_session_refused",
                {"step": result.step, "detail": result.detail, "findings": result.findings},
            )
        logger.info("scheduler.session_open_finished", opened=result.opened, step=result.step)
        # A runtime that is not live refuses on every weekday by design; alerting
        # on that would train the operator to ignore this channel.
        if result.step != "runtime":
            await send_session_open_alert(settings, result)

    return _job


def _make_reconciliation_refresh_job(settings: Settings, calendar: TradingCalendar):
    """Keep the reconciliation gate inside its fifteen-minute window all session.

    Without this the 08:45 verdict expires at 09:00 and every signal for the
    rest of the day is refused -- armed, healthy, and unable to trade. Ten
    minutes leaves five of margin against a slow broker call.
    """

    async def _job() -> None:
        from app.services.live_session_open import refresh_reconciliation

        result = await refresh_reconciliation(settings, calendar)
        if not result.ran:
            logger.debug("scheduler.reconciliation_refresh_skipped", step=result.step, detail=result.detail)
            return
        logger.info("scheduler.reconciliation_refreshed", safe=result.safe_to_trade, detail=result.detail)
        # Only a change is news. A message every ten minutes saying "still fine"
        # is one nobody reads, and this channel also carries "trading stopped".
        if not result.changed:
            return
        await _persist_audit(
            "scheduler.reconciliation_state_changed",
            {"safe_to_trade": result.safe_to_trade, "detail": result.detail},
        )
        await _send_reconciliation_alert(settings, result)

    return _job


async def _send_reconciliation_alert(settings: Settings, result) -> None:
    try:
        from app.services.telegram import TelegramNotificationService
        from app.services.telegram_config import configured_settings

        effective = await configured_settings(settings)
        if not effective.telegram_is_configured:
            return
        ist = datetime.now(UTC).astimezone(MARKET_TIMEZONE).strftime("%d-%b-%Y %I:%M %p")
        if result.safe_to_trade:
            text = (
                "\u2705 <b>Reconciliation cleared</b>\n\n"
                f"\U0001f550 {ist} IST\n"
                f"\U0001f4dd {result.detail}\n\n"
                "<i>Broker and local state agree again. Trading can resume.</i>"
            )
        else:
            findings = "".join(f"\n  \u2022 {item}" for item in result.findings[:5] if item)
            text = (
                "\u26d4 <b>Reconciliation BLOCKED — trading stopped</b>\n\n"
                f"\U0001f550 {ist} IST\n"
                f"\U0001f4dd {result.detail}{findings}\n\n"
                "<i>No further orders can be placed until this clears. "
                "The activation is still armed; the gate is what is refusing.</i>"
            )
        await TelegramNotificationService(effective).send_message(text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("scheduler.reconciliation_alert_failed", error=str(exc))


def _make_exit_sweep_job(settings: Settings, calendar: TradingCalendar):
    """Close positions that have reached their target or their square-off time.

    Every minute, because a square-off at 15:15 that happens at 15:20 is not a
    square-off. The sweep is inert on a paper deployment and when the exchange
    is closed, and it deliberately does not check whether the system is armed:
    disarming stops new entries, and a position already open still has to be
    closeable.
    """

    async def _job() -> None:
        from app.services.live_exit_manager import sweep_live_exits

        result = await sweep_live_exits(settings, calendar)
        if not result.ran:
            logger.debug("scheduler.exit_sweep_skipped", step=result.step, detail=result.detail)
            return
        logger.info("scheduler.exit_swept", detail=result.detail)
        for item in result.noteworthy:
            logger.info(
                "scheduler.exit_decision", symbol=item.symbol, step=item.step, acted=item.acted, detail=item.detail
            )
            await _persist_audit(
                "scheduler.live_exit", {"symbol": item.symbol, "step": item.step, "detail": item.detail}
            )
        if result.noteworthy:
            await _send_exit_alert(settings, result)

    return _job


async def _send_exit_alert(settings: Settings, result) -> None:
    """Only what the operator needs to read. A sweep that held everything is silent."""
    try:
        from app.services.telegram import TelegramNotificationService
        from app.services.telegram_config import configured_settings

        effective = await configured_settings(settings)
        if not effective.telegram_is_configured:
            return
        ist = datetime.now(UTC).astimezone(MARKET_TIMEZONE).strftime("%d-%b-%Y %I:%M %p")
        lines = []
        for item in result.noteworthy:
            icon = "\u2705" if item.acted else "\U0001f6a8"
            lines.append(f"{icon} <b>{item.symbol}</b> — {item.detail}")
        body = "\n".join(lines)
        problems = [item for item in result.noteworthy if not item.acted]
        heading = "\U0001f6a8 <b>EXIT NEEDS ATTENTION</b>" if problems else "\u2705 <b>POSITION CLOSED</b>"
        await TelegramNotificationService(effective).send_message(
            f"{heading}\n\n\U0001f550 {ist} IST\n{body}", parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("scheduler.exit_alert_failed", error=str(exc))


def init_upstox_scheduler(settings: Settings) -> AsyncIOScheduler | None:
    """Create and configure the APScheduler instance.

    Returns ``None`` if auto-auth is not configured, so callers can skip start/stop.
    """
    calendar = TradingCalendar.from_settings(settings)
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    if settings.upstox_auto_auth_is_configured:
        # Primary job: 08:30 AM IST, Monday–Friday
        scheduler.add_job(
            _make_job_func(settings, calendar),
            trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone="Asia/Kolkata"),
            id="upstox_morning_renewal",
            name="Upstox Morning Token Renewal (08:30 IST)",
            replace_existing=True,
            misfire_grace_time=3600,  # allow up to 1 hour late if container was down
        )
    else:
        logger.info("scheduler.auto_auth_disabled", reason="Not all UPSTOX_AUTO_AUTH fields are configured")

    # After the close and after settlement has had time to happen. The job skips
    # itself on a day with no live orders, so it costs a paper deployment one
    # indexed query an evening.
    scheduler.add_job(
        _make_broker_figures_job(settings),
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Kolkata"),
        id="broker_day_figures",
        name="Broker day figures (18:00 IST)",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    # 08:45, after the 08:30 renewal has had time to finish and well before the
    # 09:15 open. The job is inert unless the runtime is configured for live.
    scheduler.add_job(
        _make_session_open_job(settings, calendar),
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone="Asia/Kolkata"),
        id="live_session_open",
        name="Live session open (08:45 IST)",
        replace_existing=True,
        # Deliberately short: a container that comes up at 11:00 must not decide
        # the morning's reconcile is still good enough to arm on.
        misfire_grace_time=900,
    )

    # Every ten minutes while the exchange is open. The job is inert unless the
    # deployment is live AND armed, so a paper or disarmed one spends nothing.
    scheduler.add_job(
        _make_reconciliation_refresh_job(settings, calendar),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/10", timezone="Asia/Kolkata"),
        id="live_reconciliation_refresh",
        name="Live reconciliation refresh (every 10 min, 09:00-15:59 IST)",
        replace_existing=True,
        # A refresh that is late has already been overtaken by the next one.
        misfire_grace_time=120,
        max_instances=1,
    )

    # Every minute while the exchange is open. A square-off at 15:15 that happens
    # at 15:20 is not a square-off, and the broker's own auto-square-off runs at
    # its own time and its own price.
    scheduler.add_job(
        _make_exit_sweep_job(settings, calendar),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*", timezone="Asia/Kolkata"),
        id="live_exit_sweep",
        name="Live exit sweep (every minute, 09:00-15:59 IST)",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )

    logger.info(
        "scheduler.configured",
        auto_auth_enabled=settings.upstox_auto_auth_is_configured,
        jobs=[job.id for job in scheduler.get_jobs()],
    )
    return scheduler


async def check_and_renew_on_startup(settings: Settings) -> None:
    """If the stored token is expired or missing, attempt immediate renewal.

    Called during FastAPI lifespan startup so a server restart after 08:30
    still gets a fresh token.
    """
    if not settings.upstox_auto_auth_is_configured:
        return

    try:
        from app.services.upstox_oauth import load_access_token

        token = await load_access_token(settings)
        if token:
            logger.info("scheduler.startup_token_valid")
            return
    except Exception:
        pass  # token missing or expired → proceed with renewal

    logger.info("scheduler.startup_renewal_needed")
    calendar = TradingCalendar.from_settings(settings)
    result = await run_upstox_auto_renewal(settings, calendar, trigger="Server Startup")
    if result:
        await _publish_status_to_redis(
            settings,
            {
                "last_run_at": datetime.now(UTC).isoformat(),
                "success": True,
                "expires_at": result["expires_at"],
                "error": None,
                "trigger": "startup",
            },
        )
