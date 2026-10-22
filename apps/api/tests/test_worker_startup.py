"""The worker must not die of the first thing it says.

On 2026-10-22 the scanner worker restarted about ten times during the session.
The traceback above each restart was the same:

    redis.exceptions.BusyLoadingError: Redis is loading the dataset in memory

That is Redis reporting a condition it knows to be temporary -- it answers
every command this way while reading its dump back after a restart. The
worker's first act was an unguarded publish of its own state, placed above the
loop's try, so the error left run(), left asyncio.run(), and ended the
process. Docker restarted it into a Redis that was still loading.

The cost is not the lost announcement. Each restart re-runs the whole intraday
backfill, and until that finishes the newest candle the worker can see is
minutes old: entries fire late in a clump and the exit manager refuses to
judge a target it cannot measure.
"""

import asyncio

import pytest
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.worker import _announce_start


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, dict(fields)))


class FakeRedis:
    """Answers hset the way the real one does in the state being tested."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def hset(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


async def test_a_reachable_redis_is_told():
    redis = FakeRedis()
    logger = RecordingLogger()
    await _announce_start(redis, logger)
    assert redis.calls == 1
    assert logger.warnings == []


@pytest.mark.parametrize(
    "error",
    [
        BusyLoadingError("Redis is loading the dataset in memory"),
        RedisConnectionError("Error 111 connecting to redis:6379. Connection refused."),
        TimeoutError("timed out"),
    ],
)
async def test_redis_being_unready_does_not_end_the_worker(error: Exception):
    """Each of these is a reason to try again shortly, not a reason to exit.

    BusyLoadingError is the one that actually happened; the other two reach
    the same line by the same route, and a guard that only caught the observed
    one would be a guard against last week.
    """
    logger = RecordingLogger()
    await _announce_start(FakeRedis(error), logger)
    assert [event for event, _ in logger.warnings] == ["scanner.worker_state_announce_failed"]
    assert type(error).__name__ in logger.warnings[0][1]["error"]


async def test_the_failure_is_said_out_loud_not_swallowed():
    """A silent suppress would make the next restart look causeless, which is
    how an hour went into finding this one."""
    logger = RecordingLogger()
    await _announce_start(FakeRedis(BusyLoadingError("loading")), logger)
    event, fields = logger.warnings[0]
    assert event == "scanner.worker_state_announce_failed"
    assert "loading" in fields["error"]


async def test_a_cancelled_worker_still_stops():
    """Shutdown must not be caught here: the container's stop signal cancels
    the task, and a guard that swallowed CancelledError would hold the worker
    open until Docker killed it."""
    with pytest.raises(asyncio.CancelledError):
        await _announce_start(FakeRedis(asyncio.CancelledError()), RecordingLogger())


def test_the_announcement_sits_above_the_loop_on_purpose():
    """Guarding it is the fix; moving it inside the loop is not.

    It reports that this process is new, which is true once. Inside the loop
    it would republish restart_count 0 over whatever the loop had recorded.
    """
    import inspect

    from app import worker

    source = inspect.getsource(worker.run)
    announce = source.index("_announce_start")
    assert announce < source.index("while True"), "the announcement runs once, before the loop"
    assert '_publish_worker_state(redis, "RUNNING", "Scanner worker supervisor started"' not in source, (
        "the startup publish goes through _announce_start, which is the guarded path"
    )
