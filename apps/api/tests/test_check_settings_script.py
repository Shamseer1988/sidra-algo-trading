"""The exposure the report prints has to be the exposure the sizer takes.

``check_settings.py`` answers "are the numbers I set the numbers that will be
used". It divided the risk budget by the stop distance and called the result
exposure -- but a LIMIT entry is sized from the price the order may fill at, so
the engine divides by a wider number. The script read 12,500 where two real
trades came in near 11,364, and an operator reading it was 10% wrong about what
one trade puts on the table.

So the agreement is pinned here against the real sizer rather than against
another copy of the arithmetic: ``plan_entry`` decides the quantity, and the
quantity this script implies must be the same one.
"""

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.entry_pricing import plan_entry

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_settings  # noqa: E402

TICK = Decimal("0.05")


def controls(**overrides) -> SimpleNamespace:
    saved = {
        "min_stop_distance_percent": 0.8,
        "live_entry_order_type": "LIMIT",
        "entry_slippage_cap_percent": 0.0,
        "entry_slippage_cap_r": 0.1,
    }
    return SimpleNamespace(**{**saved, **overrides})


def implied_quantity(saved: SimpleNamespace, entry: Decimal, budget: Decimal) -> int:
    """What the script's own figure says the position will be."""
    return int(budget / (entry * check_settings.sizing_room(saved)))


@pytest.mark.parametrize("entry", [Decimal("100"), Decimal("1000"), Decimal("1258.40")])
@pytest.mark.parametrize(
    "saved",
    [
        controls(),
        controls(entry_slippage_cap_percent=0.05),
        controls(entry_slippage_cap_r=0.0, entry_slippage_cap_percent=0.05),
        controls(min_stop_distance_percent=0.35),
    ],
    ids=["r-cap", "both-caps", "percent-cap-only", "tighter-floor"],
)
def test_the_quantity_the_report_implies_is_the_quantity_the_sizer_takes(
    saved: SimpleNamespace, entry: Decimal
) -> None:
    """A bound, in the direction that is safe to be wrong in.

    The script has no share price, so it cannot know that the engine rounds the
    capped limit *down* to the instrument's tick before dividing -- which gives
    back part of the drift, and all of it on a share cheap enough that the
    drift is under one tick. There is therefore no exact figure to assert, and
    tuning a tolerance until it passes would only hide that. What is true for
    every price is two-sided: the reported figure never claims a larger
    position than the sizer takes, and the bare stop is the other end. The
    report prints both, so the operator reads the range rather than a false
    precision. Landing above the upper bound would be the old bug back again.
    """
    budget = Decimal("100")
    floor = Decimal(str(saved.min_stop_distance_percent)) / 100
    stop = entry * (Decimal("1") - floor)
    plan = plan_entry(
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        # Larger than anything the budget can afford, so ``min(planned,
        # affordable)`` cannot be what makes the two agree.
        quantity=100_000,
        risk_budget=budget,
        cap_percent=Decimal(str(saved.entry_slippage_cap_percent)),
        cap_r=Decimal(str(saved.entry_slippage_cap_r)),
        tick=TICK,
    )
    assert plan.refusal is None

    reported = implied_quantity(saved, entry, budget)
    from_the_stop_alone = int(budget / (entry * floor))
    assert reported <= plan.quantity <= from_the_stop_alone + 1


def test_a_market_entry_is_sized_from_the_stop_itself() -> None:
    """Nothing caps or re-sizes a MARKET entry, so the wider room would be
    wrong in the other direction."""
    assert check_settings.sizing_room(controls(live_entry_order_type="MARKET")) == Decimal("0.008")


def test_the_tighter_of_the_two_caps_decides() -> None:
    """The engine takes min() of the two ceilings, and so must this."""
    assert check_settings.sizing_room(controls()) == Decimal("0.008") + Decimal("0.0008")
    assert check_settings.sizing_room(controls(entry_slippage_cap_percent=0.05)) == Decimal("0.0085")


def test_a_cap_of_zero_is_a_cap_that_is_off() -> None:
    saved = controls(entry_slippage_cap_percent=0.0, entry_slippage_cap_r=0.0)
    assert check_settings.sizing_room(saved) == Decimal("0.008")


def test_a_drift_too_small_to_reach_one_tick_is_never_paid() -> None:
    """The case the two-sided claim above exists for, stated once on its own.

    A 0.35% stop on a 100 share is 35 paise, and a tenth of that is 3.5 paise
    against a 5 paise tick: the capped limit rounds back to the signal's own
    price, so the engine sizes 285 shares where the report implies 259. The
    report is conservative there, not wrong in the direction that matters -- it
    never claims a larger position than the sizer will take.
    """
    saved = controls(min_stop_distance_percent=0.35)
    entry, budget = Decimal("100"), Decimal("100")
    plan = plan_entry(
        side="BUY",
        entry_price=entry,
        stop_price=Decimal("99.65"),
        quantity=100_000,
        risk_budget=budget,
        cap_percent=Decimal("0"),
        cap_r=Decimal("0.1"),
        tick=TICK,
    )
    assert plan.limit_price == entry
    assert plan.quantity == 285
    assert implied_quantity(saved, entry, budget) == 259
