"""Replaying only what the live scanner would have scanned.

The replay has no universe gate, so a backtest trades every instrument it is
handed -- including the low-ATR large caps the daily ranking refuses. On
13 October those were BANKBARODA, BHARTIARTL, ICICIBANK, RELIANCE and SBIN:
exactly the shares where a 1.2% target sits outside the day's range, which is
the failure the ATR band exists to prevent. The 60-session sweep that found
every setting unprofitable included all of them, so its verdict may be a
verdict on instruments the system never trades.

``--ranked-only`` removes them. It needs no engine change: the replay skips
any session an instrument has fewer than three candles for, so dropping the
candles for a rejected session is the same as gating it.

What is tested here is the filter, not the ranking. ``rank_universe`` is live
code with its own tests; these replace it with a known answer and check that
the candles kept are exactly the ones it selected -- which is the contract the
rest of the sweep depends on.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.market_calculations import CompletedCandle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_sweep  # noqa: E402

pytestmark = pytest.mark.anyio

LOUD = "NSE_EQ|FILTER-LOUD"
QUIET = "NSE_EQ|FILTER-QUIET"
SESSIONS = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def candles(token: str, day: date, count: int = 10) -> list[CompletedCandle]:
    start = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)
    return [
        CompletedCandle(
            instrument_token=token,
            timeframe_seconds=60,
            opened_at=start + timedelta(minutes=index),
            closed_at=start + timedelta(minutes=index + 1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1_000,
            tick_count=1,
        )
        for index in range(count)
    ]


def inputs() -> dict[str, list[CompletedCandle]]:
    return {token: [c for day in SESSIONS for c in candles(token, day)] for token in (LOUD, QUIET)}


def settings():
    """The real Settings, since UniverseControls reads a dozen fields off it.
    Only the history length matters to the filter itself."""
    from app.core.config import get_settings

    return get_settings()


def ranking(selected_per_call: list[set[str]]):
    """Stands in for rank_universe, answering a scripted selection per session."""
    calls = iter(selected_per_call)

    def rank(daily_by_instrument, session_opens, controls):
        chosen = next(calls)
        return [
            SimpleNamespace(instrument_token=token, selected=token in chosen, rejection_reason=None)
            for token in daily_by_instrument
        ]

    return rank


def install(monkeypatch: pytest.MonkeyPatch, selected_per_call: list[set[str]]) -> None:
    from app.services import universe

    monkeypatch.setattr(universe, "rank_universe", ranking(selected_per_call))

    async def stored():
        return None

    monkeypatch.setattr(universe, "_stored_controls", stored)


async def test_only_the_selected_instrument_keeps_its_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract. Sessions 2 and 3 are ranked; the quiet one is refused on
    both, so none of its candles from those sessions survive."""
    install(monkeypatch, [{LOUD}, {LOUD}])
    kept = await run_sweep._ranked_only(inputs(), settings())

    assert set(kept) == {LOUD, QUIET}
    # The first session cannot be ranked -- nothing precedes it -- so both keep
    # it, and only the loud one keeps the two that could be.
    assert {candle.session_date for candle in kept[LOUD]} == set(SESSIONS)
    assert {candle.session_date for candle in kept[QUIET]} == {SESSIONS[0]}


async def test_the_first_session_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no prior history to rank the first stored session on. The live
    gate fails open in that position, so this does too -- dropping it would
    silently shrink the window the sweep reports on."""
    install(monkeypatch, [set(), set()])
    kept = await run_sweep._ranked_only(inputs(), settings())
    for token in (LOUD, QUIET):
        assert {candle.session_date for candle in kept[token]} == {SESSIONS[0]}


async def test_an_instrument_refused_everywhere_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    """One with nothing left is dropped from the dict rather than left as an
    empty list, so the sweep's instrument count is the truth."""
    only_loud = {token: rows for token, rows in inputs().items() if token == LOUD}
    install(monkeypatch, [set(), set()])
    kept = await run_sweep._ranked_only({**only_loud, QUIET: [c for c in candles(QUIET, SESSIONS[1])]}, settings())
    assert QUIET not in kept
    assert LOUD in kept


async def test_the_selection_can_differ_between_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ranking is rebuilt daily, so a share refused on Monday and accepted
    on Tuesday must be absent from one session and present in the other. A
    filter applied once for the whole range would miss this."""
    install(monkeypatch, [{LOUD}, {QUIET}])
    kept = await run_sweep._ranked_only(inputs(), settings())
    assert {candle.session_date for candle in kept[LOUD]} == {SESSIONS[0], SESSIONS[1]}
    assert {candle.session_date for candle in kept[QUIET]} == {SESSIONS[0], SESSIONS[2]}
