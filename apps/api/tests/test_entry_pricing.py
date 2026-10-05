"""The worst price an entry may pay, and how much may be bought there.

Written from a live trade. On 5 October a signal planned ₹985.85 behind a stop
at ₹977.96 -- ₹7.89 a share, twelve shares, ₹95 of a ₹100 budget. It went as a
MARKET order and filled at ₹995.40, which against the same stop is ₹17.44 a
share: ₹209 at risk on a ₹100 budget, decided entirely by what the market did
in the seconds after the candle closed.

The budget had no mechanism behind it. These are the tests for the mechanism.
"""

from decimal import Decimal

import pytest

from app.services.entry_pricing import DEFAULT_CAP_PERCENT, plan_entry
from app.services.price_ticks import is_on_tick

# The trade that prompted all of this.
BAJFINANCE = dict(
    side="BUY",
    entry_price=Decimal("985.85"),
    stop_price=Decimal("977.9632"),
    quantity=12,
    risk_budget=Decimal("100"),
)


def test_the_trade_that_went_over_budget():
    """At the capped price the same budget buys nine shares, not twelve.

    And the cap is ₹988.30, so the ₹995.40 the market actually offered that
    morning would not have been taken at all.
    """
    plan = plan_entry(**BAJFINANCE)
    assert plan.limit_price == Decimal("988.30")
    assert plan.quantity == 9
    assert plan.planned_quantity == 12
    assert plan.reduced is True
    assert plan.worst_case_risk <= Decimal("100")
    assert Decimal("995.40") > plan.limit_price


@pytest.mark.parametrize("cap", [Decimal("0"), Decimal("0.1"), DEFAULT_CAP_PERCENT, Decimal("1"), Decimal("5")])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_a_fill_at_the_cap_never_exceeds_the_budget(cap, side):
    """The property the whole module exists for, at every cap it allows.

    Not "usually within" -- a fill anywhere inside the cap risks no more than
    the trade was given, because the size was computed from the worst price
    rather than the hoped-for one.
    """
    entry, stop = (Decimal("1000"), Decimal("980")) if side == "BUY" else (Decimal("1000"), Decimal("1020"))
    plan = plan_entry(
        side=side, entry_price=entry, stop_price=stop, quantity=50, risk_budget=Decimal("100"), cap_percent=cap
    )
    if plan.refusal:
        return
    room = abs(plan.limit_price - stop)
    assert room * plan.quantity <= Decimal("100")
    assert plan.worst_case_risk <= Decimal("100")


def test_a_limit_alone_would_not_have_been_enough():
    """Capping the price without resizing still breaks the budget.

    The loss is price times quantity. Twelve shares at the capped ₹988.30 would
    risk ₹124 -- better than ₹209 and still over. The sizing is not a refinement
    of the cap, it is the other half of it.
    """
    plan = plan_entry(**BAJFINANCE)
    unsized = (plan.limit_price - BAJFINANCE["stop_price"]) * BAJFINANCE["quantity"]
    assert unsized > Decimal("100")
    assert plan.worst_case_risk <= Decimal("100")


# --- direction ------------------------------------------------------------


def test_a_buy_may_pay_more_and_a_sell_may_receive_less():
    buy = plan_entry(
        side="BUY",
        entry_price=Decimal("1000"),
        stop_price=Decimal("980"),
        quantity=5,
        risk_budget=Decimal("200"),
        cap_percent=Decimal("1"),
    )
    sell = plan_entry(
        side="SELL",
        entry_price=Decimal("1000"),
        stop_price=Decimal("1020"),
        quantity=5,
        risk_budget=Decimal("200"),
        cap_percent=Decimal("1"),
    )
    assert buy.limit_price > Decimal("1000")
    assert sell.limit_price < Decimal("1000")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_the_limit_is_a_price_the_exchange_accepts(side):
    entry, stop = (Decimal("985.85"), Decimal("977.96")) if side == "BUY" else (Decimal("985.85"), Decimal("993.70"))
    plan = plan_entry(side=side, entry_price=entry, stop_price=stop, quantity=12, risk_budget=Decimal("100"))
    assert is_on_tick(plan.limit_price)


def test_a_zero_cap_is_the_strictest_setting_not_a_disabled_one():
    plan = plan_entry(**{**BAJFINANCE, "cap_percent": Decimal("0")})
    assert plan.limit_price == Decimal("985.85")
    assert plan.refusal is None


# --- refusals -------------------------------------------------------------


def test_a_stop_on_the_wrong_side_sends_nothing():
    """A long whose stop sits above its entry is a malformed signal.

    Sizing it would divide by a negative number and hand back an enormous
    quantity, which is the shape of bug that empties an account. The cap itself
    can never cause this -- a buy's limit moves up while its stop is below -- so
    this guards the signal, not the arithmetic above it.
    """
    plan = plan_entry(
        side="BUY",
        entry_price=Decimal("1000"),
        stop_price=Decimal("1010"),
        quantity=20,
        risk_budget=Decimal("100"),
    )
    assert plan.quantity == 0
    assert plan.refusal is not None
    assert "past the stop" in plan.refusal


def test_a_wide_cap_shrinks_the_position_rather_than_refusing_it():
    """A 2% cap against a ₹5 stop is a very different trade, and a legal one.

    Twenty shares becomes four, because at ₹1020 against a ₹995 stop the budget
    only reaches that far. The system trades smaller rather than declining: the
    price is still inside what the operator said they were willing to pay.
    """
    plan = plan_entry(
        side="BUY",
        entry_price=Decimal("1000"),
        stop_price=Decimal("995"),
        quantity=20,
        risk_budget=Decimal("100"),
        cap_percent=Decimal("2"),
    )
    assert plan.refusal is None
    assert plan.quantity == 4
    assert plan.worst_case_risk == Decimal("100.00")


def test_a_budget_too_small_for_one_share_sends_nothing():
    plan = plan_entry(
        side="BUY",
        entry_price=Decimal("5000"),
        stop_price=Decimal("4000"),
        quantity=10,
        risk_budget=Decimal("50"),
    )
    assert plan.quantity == 0
    assert "does not cover one share" in (plan.refusal or "")


def test_a_signal_with_no_quantity_or_price_is_refused():
    assert (
        plan_entry(
            side="BUY",
            entry_price=Decimal("0"),
            stop_price=Decimal("1"),
            quantity=10,
            risk_budget=Decimal("100"),
        ).refusal
        is not None
    )
    assert (
        plan_entry(
            side="BUY",
            entry_price=Decimal("100"),
            stop_price=Decimal("90"),
            quantity=0,
            risk_budget=Decimal("100"),
        ).refusal
        is not None
    )


def test_a_signal_with_no_budget_is_refused():
    assert plan_entry(**{**BAJFINANCE, "risk_budget": Decimal("0")}).refusal is not None


# --- what it must not do --------------------------------------------------


def test_a_worse_price_never_justifies_a_bigger_position():
    """Spare budget is not a reason to exceed the plan.

    A wide stop and a generous budget leave room for more shares than the
    signal asked for; taking them would be this module deciding to trade
    larger than the strategy intended.
    """
    plan = plan_entry(
        side="BUY",
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        quantity=5,
        risk_budget=Decimal("10000"),
    )
    assert plan.quantity == 5
    assert plan.reduced is False


def test_the_plan_never_rounds_a_share_up():
    """A share that takes the trade one rupee over budget is one too many."""
    plan = plan_entry(
        side="BUY",
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        quantity=100,
        risk_budget=Decimal("99"),
        cap_percent=Decimal("0"),
    )
    assert plan.quantity == 9  # 99 / 10 = 9.9
    assert plan.worst_case_risk == Decimal("90.00")
