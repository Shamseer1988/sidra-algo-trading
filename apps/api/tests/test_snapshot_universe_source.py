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
