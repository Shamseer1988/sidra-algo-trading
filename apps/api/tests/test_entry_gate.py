"""The lock that makes a daily limit mean something.

Every daily gate reads state and then acts on it, and until this lock there was
nothing holding the place between the two. On 9 November four entries left for
the broker at 09:37:01 and all three daily gates passed all four, because when
each was checked none of the other three existed yet.

So the clauses tested here are: only one holder at a time, the body always runs
(a refused signal must still be written down), the lock is released on every
path including an exception, and nobody can release somebody else's.
"""

import asyncio
from datetime import date

import pytest

from app.services import entry_gate as gate_module
from app.services.entry_gate import TTL_SECONDS, entry_gate, key_for

SESSION = date(2026, 11, 9)


class FakeRedis:
    """Honours nx, records px, and performs the gate's compare-and-delete."""

    def __init__(self, *, fail_release: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.px: list[int | None] = []
        self.fail_release = fail_release
        self.releases = 0

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None, **_: object) -> bool | None:
        self.px.append(px)
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, token: str) -> int:
        self.releases += 1
        if self.fail_release is not None:
            raise self.fail_release
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


# --- one at a time --------------------------------------------------------


async def test_a_second_holder_is_refused_while_the_first_is_inside():
    redis = FakeRedis()
    async with entry_gate(redis, SESSION) as first:
        assert first.held is True
        async with entry_gate(redis, SESSION, wait=0.0) as second:
            assert second.held is False
            assert second.block_reason is not None


async def test_the_refusal_says_why_in_words_an_operator_can_act_on():
    """It goes into the evaluation row, which is where somebody looks when a
    signal they expected did not become a trade."""
    redis = FakeRedis()
    async with entry_gate(redis, SESSION), entry_gate(redis, SESSION, wait=0.0) as second:
        assert "Another entry is being placed" in second.block_reason
        assert "cannot see it yet" in second.block_reason


async def test_the_body_still_runs_when_the_lock_was_not_got():
    """A signal refused and never written down is the one an operator cannot
    find afterwards, so the gate reports rather than skips."""
    redis = FakeRedis()
    ran = []
    async with entry_gate(redis, SESSION), entry_gate(redis, SESSION, wait=0.0) as second:
        ran.append(second.held)
    assert ran == [False]


async def test_the_lock_is_free_again_afterwards():
    redis = FakeRedis()
    async with entry_gate(redis, SESSION) as first:
        assert first.held is True
    async with entry_gate(redis, SESSION, wait=0.0) as second:
        assert second.held is True


async def test_an_exception_inside_the_body_still_releases_it():
    """The body places orders. A failure there must not end the trading day."""
    redis = FakeRedis()
    with pytest.raises(RuntimeError):
        async with entry_gate(redis, SESSION):
            raise RuntimeError("submission raised")
    assert key_for(SESSION) not in redis.values


async def test_a_release_that_fails_does_not_raise():
    """It runs in a finally, around the code that places real orders."""
    redis = FakeRedis(fail_release=ConnectionError("redis went away"))
    async with entry_gate(redis, SESSION) as held:
        assert held.held is True
    assert redis.releases == 1


# --- nobody releases somebody else's -------------------------------------


async def test_a_holder_whose_lock_expired_does_not_release_the_next_one():
    """A plain DELETE would. After an expiry the key belongs to whoever took it
    next, and a lock that can be released by a non-holder is not a lock."""
    redis = FakeRedis()
    async with entry_gate(redis, SESSION):
        # Simulate the TTL expiring and a second process taking the key.
        redis.values[key_for(SESSION)] = "a-different-holder"
    assert redis.values[key_for(SESSION)] == "a-different-holder"


# --- the key and the expiry ----------------------------------------------


async def test_it_is_taken_with_an_expiry_so_a_dead_process_cannot_end_the_day():
    redis = FakeRedis()
    async with entry_gate(redis, SESSION):
        pass
    assert redis.px == [TTL_SECONDS * 1000]


def test_the_key_is_scoped_to_the_trading_date():
    """So a lock cannot be inherited by the next session, whatever happens to a
    process at 15:30."""
    assert key_for(date(2026, 11, 9)) != key_for(date(2026, 11, 10))
    assert "2026-11-09" in key_for(SESSION)


def test_the_wait_is_well_below_the_expiry():
    """A waiter blocks the coroutine that also feeds candles, so it must not be
    able to wait out a broker timeout -- the Upstox order client allows 15s."""
    assert gate_module.WAIT_SECONDS < TTL_SECONDS / 4
    assert gate_module.WAIT_SECONDS >= 5


# --- it serialises rather than losing one --------------------------------


async def test_four_signals_at_one_candle_boundary_pass_through_one_at_a_time():
    """9 November's shape: four entries in the same second. They are not
    refused outright -- each takes its turn, and the limits inside then decide
    which of them may trade."""
    redis = FakeRedis()
    order: list[str] = []

    async def arrive(name: str) -> None:
        async with entry_gate(redis, SESSION) as held:
            assert held.held is True, f"{name} was refused"
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(*(arrive(name) for name in ("sbin", "pnb", "bankbaroda", "vedl")))

    assert len(order) == 8
    # Never two inside at once: every "in" is immediately followed by its "out".
    for index in range(0, 8, 2):
        assert order[index].endswith("-in")
        assert order[index + 1] == order[index].replace("-in", "-out")
