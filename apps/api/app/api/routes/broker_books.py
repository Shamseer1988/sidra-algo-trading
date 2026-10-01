"""The broker's order book and position book, read-only, for a screen.

Separate from ``broker.py``, which configures which broker to use, and from
``live.py``, which reports the readiness gates. This router does one thing:
hand the UI what the broker currently says about the account.

**Nothing here writes.** No placement, modification or cancellation, and the
only adapter methods called are the read-only pair reconciliation already uses.
A screen that could cancel an order is a screen one misplaced tap can empty an
account with, and that is not what was asked for.

**Reads are shared and cached.** Upstox allows 2000 reads per thirty minutes
against the same per-user budget that order placement and reconciliation draw
on, so a page polling on its own could cost a trade. ``broker_snapshot``
collapses every viewer onto one short-lived read; ``?force=true`` is the
operator asking for a fresh one and is the only way past it.
"""

from typing import Any

from fastapi import APIRouter, Query
from redis.asyncio import Redis

from app.api.deps import AppSettings, CurrentUser, DbSession

router = APIRouter(prefix="/broker-books", tags=["Broker books"])


async def _redis(settings: AppSettings) -> Redis:
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


@router.get("/snapshot")
async def broker_snapshot(
    _: CurrentUser,
    settings: AppSettings,
    session: DbSession,
    force: bool = Query(False, description="Bypass the shared cache and read the broker now."),
) -> dict[str, Any]:
    """Orders, positions and the money on them, as the broker reports it.

    One payload rather than three endpoints: the three are read in one pass at
    the broker, and splitting them would triple the rate-limit cost of a page
    that always shows all of them together.

    Always 200. A broker that cannot be reached comes back as
    ``readable: false`` with the reason in words -- a screen that receives a
    500 can only say "something went wrong", which is less than the operator
    already knew.
    """
    from app.services.broker_snapshot import read

    redis = await _redis(settings)
    try:
        return await read(session, settings, redis, force=force)
    finally:
        await redis.aclose()
