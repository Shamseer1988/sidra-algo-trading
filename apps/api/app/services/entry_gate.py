"""One entry decision at a time, account-wide.

Every daily gate in this system reads state and then acts on it: the trade
ceiling counts today's trades, the risk allocation totals today's planned
risk, the position limit reads the broker's book. Between the read and the
order there was nothing holding the place, and two things call the scanner at
once -- the market-data feed task and the supervisor's ``flush_expired()``
(``worker.py`` 291 and 315; ``candle_aggregation`` carries its own comment
about these two racing).

On 9 November four entries left for the broker in the same second, 09:37:01.
Every gate passed every one of them, because when each was checked none of the
other three existed yet:

    maximum_daily_trades        3      -> 4 taken
    maximum_daily_risk_percent  1.5%   -> 2.0% committed (500 of 375)
    maximum_open_positions      2      -> 4 held together until 12:10

Three of the four were banks. It made money, which is the only reason it was
not obvious: four stops would have been about 540 against a 500 daily loss
limit that could not have fired, because the positions were already open.

Counting in-flight orders (``LIVE_IN_FLIGHT_STATUSES``) closes most of this on
its own -- the submission row is committed *before* the request leaves, so once
it exists every gate can see it. What that does not cover is the window between
a gate's read and that insert. This lock covers it, and it is in Redis rather
than in the process because the API container can place an order too, and an
``asyncio.Lock`` would not know.

**Refusing is the safe answer, waiting briefly is the useful one.** A waiter
blocks the coroutine that is also feeding candles, so it cannot wait out a
broker timeout. It waits long enough for the holder's ordinary case -- a few
database writes and a fast round trip -- and then refuses, which costs one
signal on one candle and nothing else: the same setup is re-evaluated on the
next one, exactly as it is after any other block reason.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger("scanner")

# Long enough that it never expires under a real entry, and only ever a safety
# net: Telegram's client allows 15s and the Upstox order client 15s, so a
# worst-case section is about half this. If it ever did expire mid-entry, the
# in-flight submission row is already committed by then, so the gates fall back
# to counting it rather than to seeing nothing.
TTL_SECONDS = 75

# What a second signal will wait for the first. Deliberately far below the TTL:
# the holder's ordinary case is well under a second, four instruments at one
# candle boundary pass through in a few, and a holder stuck in a 15s broker
# timeout must not stall the feed behind it.
WAIT_SECONDS = 8.0
POLL_SECONDS = 0.05

# Compare-and-delete. Releasing with a plain DELETE would, after an expiry,
# delete whichever holder came next -- and a lock that can be released by
# somebody who does not hold it is not a lock.
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@dataclass
class EntryGate:
    """Whether this signal may proceed to the gates, and why not if it may not."""

    held: bool
    block_reason: str | None = None


def key_for(session_date: date) -> str:
    # Per trading date so a lock can never be inherited by the next session,
    # whatever happens to a process at 15:30.
    return f"scanner:entry_gate:{session_date.isoformat()}"


async def _acquire(redis: Redis, key: str, token: str, *, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while True:
        if await redis.set(key, token, nx=True, px=TTL_SECONDS * 1000):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def entry_gate(redis: Redis, session_date: date, *, wait: float = WAIT_SECONDS):
    """Hold the account's entry lock for the body, or report that it could not.

    The body always runs. A signal that did not get the lock must still be
    recorded as an evaluation with its reason, the way every other blocked
    signal is -- an entry refused and never written down is the thing an
    operator cannot find afterwards.
    """
    key = key_for(session_date)
    token = uuid.uuid4().hex
    waited_from = time.monotonic()
    held = await _acquire(redis, key, token, wait=wait)
    if not held:
        logger.warning(
            "scanner.entry_gate_busy",
            session_date=session_date.isoformat(),
            waited_seconds=round(time.monotonic() - waited_from, 2),
            detail=(
                "another entry was still being placed, so this signal was refused rather than "
                "checked against limits that cannot yet see it"
            ),
        )
        yield EntryGate(
            held=False,
            block_reason=(
                "Another entry is being placed right now. The day's limits cannot see it yet, "
                f"so this signal was refused instead of checked against them (waited "
                f"{round(time.monotonic() - waited_from, 1)}s)."
            ),
        )
        return
    try:
        yield EntryGate(held=True)
    finally:
        try:
            await redis.eval(RELEASE, 1, key, token)
        except Exception as exc:  # noqa: BLE001 - a failed release must not lose the entry
            # The TTL is the backstop, and it is why there is one. Logged
            # because until it expires the account takes no further entry, and
            # an operator watching nothing happen deserves the reason.
            logger.warning(
                "scanner.entry_gate_release_failed",
                error=f"{type(exc).__name__}: {exc}",
                detail=f"the lock will expire on its own within {TTL_SECONDS}s",
            )
