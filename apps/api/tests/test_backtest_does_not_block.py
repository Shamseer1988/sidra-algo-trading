"""The replay must not run on the event loop.

On 9 October a ten-instrument backtest starved the loop for long enough that
the container's own health check -- a three-second urlopen of /health/ready --
timed out five times in a row and Docker marked the API unhealthy. The run
itself completed correctly. Everything else stopped: every screen, the
scheduled jobs, and the exit sweep that squares positions off at the cutoff.

The replay is a tight CPU-bound pass over every candle and always will be, so
the fix is where it runs, not how fast it is. These tests assert the property
rather than the call: a coroutine that awaits a blocking function in a thread
keeps answering, and one that calls it inline does not.
"""

import asyncio
import time

import pytest

pytestmark = pytest.mark.anyio

# Long enough that an inline call cannot be mistaken for a fast one, short
# enough not to slow the suite.
WORK_SECONDS = 0.4
HEARTBEAT_SECONDS = 0.02


def a_replay() -> str:
    """Stands in for run_completed_candle_backtest: CPU-bound, no awaits."""
    deadline = time.monotonic() + WORK_SECONDS
    while time.monotonic() < deadline:
        pass
    return "done"


async def heartbeats_during(work) -> int:
    """How many times the loop got a turn while `work` was running."""
    beats = 0
    stop = False

    async def beat() -> None:
        nonlocal beats
        while not stop:
            beats += 1
            await asyncio.sleep(HEARTBEAT_SECONDS)

    ticker = asyncio.create_task(beat())
    await asyncio.sleep(0)
    try:
        await work()
    finally:
        stop = True
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker
    return beats


async def test_running_the_replay_inline_stops_the_api_answering() -> None:
    """The behaviour being fixed, pinned so the test above means something."""

    async def inline() -> str:
        return a_replay()

    assert await heartbeats_during(inline) <= 2, "an inline replay should starve the loop"


async def test_running_the_replay_in_a_thread_keeps_the_api_answering() -> None:
    async def threaded() -> str:
        return await asyncio.to_thread(a_replay)

    beats = await heartbeats_during(threaded)
    expected = WORK_SECONDS / HEARTBEAT_SECONDS
    assert beats > expected / 2, f"only {beats} turns in {WORK_SECONDS}s; the loop was still blocked"


async def test_the_route_runs_no_replay_at_all() -> None:
    """Research Lab Phase 3 moved the replay out of the API process entirely.

    A thread kept the event loop answering but still held the GIL, so the
    scheduler jobs in the same process -- the live exit sweep among them --
    ran slower while a sweep worked. The routes now enqueue a research job and
    the research-worker container runs it. Pinned by name so a future edit
    that brings the replay back into the API is caught here.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/api/routes/backtesting.py").read_text()
    for call in ("run_portfolio_backtest(", "run_parameter_sweep(", "run_completed_candle_backtest(", "to_thread("):
        assert call not in source, f"{call} is back in the API process"
    assert "research_jobs.enqueue(" in source
