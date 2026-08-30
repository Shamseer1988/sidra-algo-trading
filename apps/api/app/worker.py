"""Scanner process entry point.

Phase 1 intentionally exposes only lifecycle logging. Future scanner orchestration is
event-driven and must pause whenever market data, Redis, or PostgreSQL is unhealthy.
"""

import asyncio

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger("scanner")
    logger.info("scanner.worker_started", mode=settings.application_mode, live_trading_enabled=False)
    try:
        while True:
            await asyncio.sleep(30)
            logger.info("scanner.worker_heartbeat", status="idle")
    except asyncio.CancelledError:
        logger.info("scanner.worker_stopped")
        raise


if __name__ == "__main__":
    asyncio.run(run())
