"""The runtime section reports the switch that is actually in force.

``universe_enabled`` moved from the environment file onto the Settings screen,
and this line kept reading the environment. On 14 October it printed "no" while
the saved value was True and the ranking had selected 18 instruments that
morning -- the same report's own section 4 and section 7 both said so.

A diagnostic that contradicts the system it describes is worse than one that
leaves the line out: the operator either stops trusting the tool or acts on the
wrong value.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY
from app.db.models import ApplicationSetting
from app.db.session import SessionLocal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import snapshot  # noqa: E402


def env(enabled: bool):
    """Only the fields this function reads, so a stub cannot answer for more."""
    return type("Settings", (), {"universe_enabled": enabled})()


async def save(**overrides) -> None:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, TRADING_KEY)
        value = {**DEFAULT_TRADING_CONTROLS, **overrides}
        if row:
            row.value = value
        else:
            session.add(ApplicationSetting(key=TRADING_KEY, value=value))
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def test_the_saved_switch_beats_the_environment() -> None:
    """The 14 October case: environment off, saved on, ranking running."""
    await save(universe_enabled=True)
    assert await snapshot.saved_universe_enabled(env(False)) == (True, "saved controls")


async def test_the_saved_switch_beats_the_environment_the_other_way() -> None:
    """And an operator who turned it off must not read the old default as on."""
    await save(universe_enabled=False)
    assert await snapshot.saved_universe_enabled(env(True)) == (False, "saved controls")


async def test_with_nothing_saved_the_environment_answers_and_says_so() -> None:
    assert await snapshot.saved_universe_enabled(env(True)) == (True, "environment; nothing saved")


async def test_a_saved_row_without_the_field_falls_back_to_the_environment() -> None:
    """A row written before the switch moved does not decide it."""
    async with SessionLocal() as session:
        session.add(ApplicationSetting(key=TRADING_KEY, value={"account_capital": 10000.0}))
        await session.commit()
    assert await snapshot.saved_universe_enabled(env(True)) == (True, "environment; nothing saved")


# --- the tick-size count, and the gap that is not a fault -----------------
#
# Section 6 printed "subscribed + stored : 28" and "with a tick size : 27" with
# nothing between them. The missing one was NSE_INDEX|Nifty 50, which is never
# ordered and therefore has no price grid -- but the gap read as one instrument
# quietly broken, and the obvious remedy would have taken the benchmark out of
# the feed and scored relative strength and the NIFTY regime at zero.


def feed(benchmark: str = "NSE_INDEX|Nifty 50", vix: str = ""):
    return type(
        "Settings",
        (),
        {"upstox_nifty_benchmark_key": benchmark, "upstox_india_vix_key": vix},
    )()


def test_the_configured_benchmark_is_reference_however_it_is_spelled() -> None:
    assert snapshot.is_reference_key("NSE_INDEX|Nifty 50", feed()) is True
    # Listed by hand under a key the prefix test alone would miss.
    assert snapshot.is_reference_key("NSE_INDEX_OPT|CUSTOM", feed(benchmark="NSE_INDEX_OPT|CUSTOM")) is True


def test_an_index_is_reference_even_when_it_is_not_the_benchmark() -> None:
    assert snapshot.is_reference_key("NSE_INDEX|India VIX", feed()) is True
    assert snapshot.is_reference_key("BSE_INDEX|SENSEX", feed()) is True


def test_an_equity_is_never_reference() -> None:
    """The one that matters: a tradable key missing a tick is a real finding."""
    assert snapshot.is_reference_key("NSE_EQ|INE040A01034", feed()) is False
    assert snapshot.is_reference_key("NSE_EQ|INE066F01020", feed()) is False


def test_an_unset_vix_key_does_not_make_everything_reference() -> None:
    """An empty configured key must not match an empty-ish anything."""
    assert snapshot.is_reference_key("", feed(vix="")) is False
    assert snapshot.is_reference_key("NSE_EQ|INE002A01018", feed(vix="")) is False
