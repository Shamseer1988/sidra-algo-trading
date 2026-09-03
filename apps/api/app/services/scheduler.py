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
from app.services.trading_calendar import TradingCalendar
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


def init_upstox_scheduler(settings: Settings) -> AsyncIOScheduler | None:
    """Create and configure the APScheduler instance.

    Returns ``None`` if auto-auth is not configured, so callers can skip start/stop.
    """
    if not settings.upstox_auto_auth_is_configured:
        logger.info("scheduler.auto_auth_disabled", reason="Not all UPSTOX_AUTO_AUTH fields are configured")
        return None

    calendar = TradingCalendar.from_settings(settings)
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    # Primary job: 08:30 AM IST, Monday–Friday
    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        timezone="Asia/Kolkata",
    )
    scheduler.add_job(
        _make_job_func(settings, calendar),
        trigger=trigger,
        id="upstox_morning_renewal",
        name="Upstox Morning Token Renewal (08:30 IST)",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1 hour late if container was down
    )

    logger.info(
        "scheduler.configured",
        job="upstox_morning_renewal",
        schedule="08:30 IST Mon–Fri",
        auto_auth_enabled=True,
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
