"""Telling the operator that the scanner has stopped working.

The worker already wrote its state to Redis and logged every failed
iteration. Nobody reads either. On 29 October it logged the same connection
error every thirty seconds for four days, stored no candles, and placed no
orders on three trading days; it was found because the operator noticed the
absence of trades, not because the system said anything.

So the worker now says it, where it already says everything else.

**These never raise.** An alert that fails must not become the second
failure, and the caller is already handling the first one.

The credentials are the awkward part. ``configured_settings`` reads the
Telegram token out of the database, and the database is exactly what is
usually broken when this fires -- so the lookup is attempted, and on any
failure the environment's own token is used instead. If Telegram was only
ever configured through the screen, and the database is down, there is
nothing to send with: that is reported in the log rather than pretended
away, so a silent evening has a reason in it.
"""

from __future__ import annotations

import structlog

from app.core.config import Settings

logger = structlog.get_logger("scanner")

# Consecutive failed iterations before the operator is told. The loop's
# backoff is 2, 4, 8, 16 then 30 seconds, so five failures is about a minute:
# past a blip, and long before the ten minutes it takes to give up.
ALERT_AFTER_FAILURES = 5


async def _reachable(settings: Settings) -> Settings | None:
    """The settings to send with, or None if nothing can be sent."""
    effective = settings
    try:
        from app.services.telegram_config import configured_settings

        effective = await configured_settings(settings)
    except Exception as exc:  # noqa: BLE001 - the database is the usual casualty
        logger.warning(
            "scanner.alert_credentials_fell_back_to_environment",
            error=f"{type(exc).__name__}: {exc}",
            detail="stored Telegram credentials could not be read; using the environment's",
        )
    if not effective.telegram_is_configured:
        logger.warning(
            "scanner.alert_not_sent_telegram_unconfigured",
            detail="the worker is degraded and there is no Telegram token to say so with",
        )
        return None
    return effective


async def _send(settings: Settings, text: str, event: str) -> None:
    effective = await _reachable(settings)
    if effective is None:
        return
    try:
        from app.services.telegram import TelegramNotificationService

        await TelegramNotificationService(effective).send_message(text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - an alert must not become a second failure
        logger.warning("scanner.alert_send_failed", event_name=event, error=f"{type(exc).__name__}: {exc}")


async def alert_worker_degraded(settings: Settings, *, error: str, failures: int) -> None:
    """Every scanner iteration is failing. Said once, about a minute in."""
    await _send(
        settings,
        "\U0001f6a8 <b>SCANNER STOPPED WORKING</b>\n\n"
        f"Every scan has failed {failures} times in a row.\n\n"
        f"<code>{error}</code>\n\n"
        "<i>No candles are being stored, so no signal can form and no order can be placed. "
        "The worker will restart itself shortly; if this repeats, it is not a blip.</i>",
        "degraded",
    )


async def alert_worker_recovered(settings: Settings, *, after_failures: int) -> None:
    """It came back on its own. Said so the earlier alarm is closed."""
    await _send(
        settings,
        "✅ <b>Scanner recovered</b>\n\n"
        f"Scanning again after {after_failures} failed attempt(s). Candles are being stored.",
        "recovered",
    )


async def alert_worker_giving_up(settings: Settings, *, error: str, failures: int) -> None:
    """It is handing the process back. Said before it goes."""
    await _send(
        settings,
        "\U0001f534 <b>SCANNER RESTARTING ITSELF</b>\n\n"
        f"{failures} consecutive failures. Exiting so the supervisor starts it fresh.\n\n"
        f"<code>{error}</code>\n\n"
        "<i>If no recovery message follows within a few minutes, the scanner is down "
        "and today's session will take no trades.</i>",
        "giving_up",
    )
