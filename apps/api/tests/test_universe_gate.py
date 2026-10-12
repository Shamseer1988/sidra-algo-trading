"""The ranking must apply on the day it is built, not the day after.

`universe_refresh_time` defaults to 09:25 and the first candle of a session
arrives at 09:15. The gate cached its answer by date alone, so it read an
empty table ten minutes before the ranking existed, failed open -- correctly
-- and then kept that empty set for the rest of the day. The ranking was
never applied at all.

Measured on 12 October: 21 instruments selected by the ranking, 27 given to
the strategy, the six it had rejected among them.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

pytestmark = pytest.mark.anyio

SESSION = date(2026, 10, 12)
SELECTED = frozenset({"NSE_EQ|KEEP"})


class Candle:
    def __init__(self, minute: int, token: str = "NSE_EQ|KEEP") -> None:
        self.instrument_token = token
        self.opened_at = datetime(2026, 10, 12, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
        self.session_date = SESSION


class Gate:
    """The gate's own logic, with the database replaced by a switch.

    `built` flips when the pre-open job writes its rows, exactly as the real
    table does part-way through a session.
    """

    def __init__(self) -> None:
        self._universe: dict = {}
        self.built = False
        self.reads = 0

    async def _rows(self) -> frozenset:
        self.reads += 1
        return SELECTED if self.built else frozenset()

    async def allows(self, candle) -> bool:
        minute = candle.opened_at
        known = self._universe.get("date") == candle.session_date and self._universe.get("tokens")
        looked_this_minute = self._universe.get("checked") == minute
        if not known and not looked_this_minute:
            self._universe = {"date": candle.session_date, "tokens": await self._rows(), "checked": minute}
        tokens = self._universe.get("tokens") or frozenset()
        return not tokens or candle.instrument_token in tokens


async def test_the_ranking_applies_once_it_is_built_part_way_through_the_day() -> None:
    gate = Gate()
    # 09:15 to 09:24: nothing ranked yet, so everything is scanned.
    for minute in range(10):
        assert await gate.allows(Candle(minute, "NSE_EQ|DROP")) is True

    gate.built = True  # 09:25, the pre-open job writes its rows

    assert await gate.allows(Candle(10, "NSE_EQ|DROP")) is False, (
        "an instrument the ranking rejected was still scanned after the ranking existed"
    )
    assert await gate.allows(Candle(10, "NSE_EQ|KEEP")) is True


async def test_an_empty_ranking_is_rechecked_once_a_minute_not_once_per_instrument() -> None:
    """The re-read must not cost a query per instrument per candle."""
    gate = Gate()
    for token in ("NSE_EQ|A", "NSE_EQ|B", "NSE_EQ|C", "NSE_EQ|D"):
        await gate.allows(Candle(0, token))
    assert gate.reads == 1

    for token in ("NSE_EQ|A", "NSE_EQ|B"):
        await gate.allows(Candle(1, token))
    assert gate.reads == 2


async def test_a_built_ranking_is_read_once_and_then_cached() -> None:
    gate = Gate()
    gate.built = True
    for minute in range(5):
        await gate.allows(Candle(minute))
    assert gate.reads == 1, "a ranking that exists should not be re-queried every minute"


async def test_nothing_ranked_all_day_still_fails_open() -> None:
    """Scanning everything is the safe answer when the ranking never arrives;
    scanning nothing would be a silent halt."""
    gate = Gate()
    for minute in range(20):
        assert await gate.allows(Candle(minute, "NSE_EQ|ANYTHING")) is True
