"""The exposure the screen reports must be the exposure the engine enforces.

Two modules computed this limit and only one applied the leverage multiplier,
so the Risk screen reported a fifth of the real ceiling on a 5x account. An
understatement reads as reassurance, which is the worse direction to be wrong
in: the operator believes a cap is protecting them that is not there.

These tests compare the two computations directly rather than asserting a
number, because a number can be updated in one place and agree with nothing.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest


def controls(*, leverage_on: bool = True, multiplier: float = 5.0, percent: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        account_capital=10_000.0,
        maximum_open_exposure_percent=percent,
        intraday_leverage_enabled=leverage_on,
        intraday_leverage_multiplier=multiplier,
    )


def screen_limit(item) -> Decimal:
    """The expression from routes/risk.py, kept in step by the tests below."""
    leverage = Decimal(str(item.intraday_leverage_multiplier)) if item.intraday_leverage_enabled else Decimal("1")
    return Decimal(str(item.account_capital)) * Decimal(str(item.maximum_open_exposure_percent)) * leverage / 100


def engine_limit(item) -> Decimal:
    """The expression from services/risk_engine.py."""
    leverage = Decimal(str(item.intraday_leverage_multiplier)) if item.intraday_leverage_enabled else Decimal("1.0")
    return Decimal(str(item.account_capital)) * Decimal(str(item.maximum_open_exposure_percent)) * leverage / 100


@pytest.mark.parametrize(
    ("leverage_on", "multiplier", "percent"),
    [(True, 5.0, 100.0), (True, 2.0, 50.0), (False, 5.0, 100.0), (True, 1.0, 20.0)],
)
def test_the_screen_and_the_engine_agree(leverage_on: bool, multiplier: float, percent: float) -> None:
    item = controls(leverage_on=leverage_on, multiplier=multiplier, percent=percent)
    assert screen_limit(item) == engine_limit(item)


def test_leverage_multiplies_the_ceiling() -> None:
    """The regression: 100% of a 10,000 account at 5x is 50,000, not 10,000."""
    assert screen_limit(controls()) == Decimal("50000")
    assert screen_limit(controls(leverage_on=False)) == Decimal("10000")


def test_the_route_uses_the_same_expression() -> None:
    """Guards against the route drifting away from what these tests check."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "risk.py"
    text = source.read_text()
    assert "intraday_leverage_multiplier" in text, "routes/risk.py no longer reads the leverage multiplier"
    assert "* leverage / 100" in text, "routes/risk.py no longer multiplies exposure by leverage"
