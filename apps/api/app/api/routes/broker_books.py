"""The broker's order book and position book, read-only, for a screen.

Separate from ``broker.py``, which configures which broker to use, and from
``live.py``, which reports the readiness gates. This router does one thing:
hand the UI what the broker currently says about the account.

**Nothing here writes.** The service behind it holds the read-only adapter,
whose client has no placement method on it at all. A screen one misplaced tap
can empty an account with is not what was asked for, and a test scans this
file's own code for anything that could change an order.

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
    broker: str | None = Query(None, description="Look at a named broker instead of the selected one."),
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

    ``broker`` names one to look at; omitted, it is the broker selected for
    trading. Naming one is a read and changes nothing about where an order
    would go.
    """
    from app.services.broker_snapshot import read

    redis = await _redis(settings)
    try:
        return await read(session, settings, redis, broker=broker, force=force)
    finally:
        await redis.aclose()


@router.get("/brokers")
async def broker_choices(
    _: CurrentUser,
    settings: AppSettings,
    session: DbSession,
) -> dict[str, Any]:
    """The brokers whose books can be shown, and which one trades.

    Separate from ``/market-data/brokers``, which is about feeds, and from the
    trading controls, which is where the live broker is actually chosen. This
    answers only "what can this screen show me", and contacts no broker to do
    it, so opening the selector costs nothing from the rate-limit budget.
    """
    from app.services.broker_snapshot import choices

    return await choices(session, settings)
