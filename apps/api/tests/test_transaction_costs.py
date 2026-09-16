"""Simulated transaction costs, and the one charge that is not linear.

Every charge except brokerage is a flat percentage, so splitting an order into
fills cannot change the total. Brokerage is capped per order, which means the
simulator has to know that eleven fills are still one order. Getting that wrong
is invisible at a large position size, where the cap binds on the first slice
anyway, and dominates every other cost at a small one — which is exactly the
size a first live account trades at.
"""

from decimal import Decimal

import pytest

from app.services.paper_execution import PaperExecutionControls, transaction_costs


def controls(**overrides) -> PaperExecutionControls:
    return PaperExecutionControls(**overrides)


def total_across_fills(price: Decimal, slices: list[int], side: str, ctrl: PaperExecutionControls) -> Decimal:
    """Fill an order in slices, accumulating gross as the executor does."""
    prior_gross = Decimal("0")
    total = Decimal("0")
    for quantity in slices:
        costs = transaction_costs(price, quantity, side, ctrl, prior_gross)
        total += costs.brokerage
        prior_gross += price * quantity
    return total


def test_one_fill_charges_the_percentage_below_the_cap() -> None:
    costs = transaction_costs(Decimal("100"), 100, "BUY", controls())
    # 0.03% of 10,000 = 3.00, well under the 20 cap.
    assert costs.brokerage == Decimal("3")


def test_one_fill_is_limited_by_the_cap() -> None:
    costs = transaction_costs(Decimal("1000"), 1000, "BUY", controls())
    # 0.03% of 1,000,000 = 300, capped at 20.
    assert costs.brokerage == Decimal("20")


def test_splitting_an_order_does_not_change_its_brokerage() -> None:
    """The property that was wrong: eleven fills are still one order."""
    ctrl = controls()
    whole = transaction_costs(Decimal("100"), 1100, "BUY", ctrl).brokerage
    sliced = total_across_fills(Decimal("100"), [100] * 11, "BUY", ctrl)
    assert whole == sliced


def test_splitting_a_capped_order_does_not_multiply_the_cap() -> None:
    """The old behaviour charged the 20 cap once per slice."""
    ctrl = controls()
    sliced = total_across_fills(Decimal("1000"), [1000] * 11, "BUY", ctrl)
    assert sliced == Decimal("20")


@pytest.mark.parametrize("slices", [[1] * 50, [10] * 5, [7, 13, 30], [49, 1], [1, 49]])
def test_brokerage_is_invariant_to_how_the_order_is_sliced(slices: list[int]) -> None:
    ctrl = controls()
    whole = transaction_costs(Decimal("500"), sum(slices), "BUY", ctrl).brokerage
    assert total_across_fills(Decimal("500"), slices, "BUY", ctrl) == whole


def test_a_slice_after_the_cap_is_reached_is_charged_nothing() -> None:
    """Once the order has paid the cap, further fills add no brokerage."""
    ctrl = controls()
    first = transaction_costs(Decimal("1000"), 1000, "BUY", ctrl, Decimal("0"))
    second = transaction_costs(Decimal("1000"), 1000, "BUY", ctrl, Decimal("1000000"))
    assert first.brokerage == Decimal("20")
    assert second.brokerage == Decimal("0")


def test_brokerage_is_never_negative() -> None:
    """A prior gross beyond the cap must not produce a credit."""
    costs = transaction_costs(Decimal("1"), 1, "BUY", controls(), Decimal("99999999"))
    assert costs.brokerage >= Decimal("0")


# --- the linear charges are unaffected -----------------------------------


def test_the_percentage_charges_do_not_depend_on_prior_fills() -> None:
    ctrl = controls()
    first = transaction_costs(Decimal("100"), 100, "SELL", ctrl, Decimal("0"))
    later = transaction_costs(Decimal("100"), 100, "SELL", ctrl, Decimal("500000"))
    assert first.stt == later.stt
    assert first.exchange_charge == later.exchange_charge
    assert first.sebi_charge == later.sebi_charge


def test_stt_is_charged_on_the_sell_side_only() -> None:
    ctrl = controls()
    assert transaction_costs(Decimal("100"), 100, "BUY", ctrl).stt == Decimal("0")
    assert transaction_costs(Decimal("100"), 100, "SELL", ctrl).stt > Decimal("0")


def test_stamp_duty_is_charged_on_the_buy_side_only() -> None:
    ctrl = controls()
    assert transaction_costs(Decimal("100"), 100, "BUY", ctrl).stamp_duty > Decimal("0")
    assert transaction_costs(Decimal("100"), 100, "SELL", ctrl).stamp_duty == Decimal("0")


def test_gst_follows_the_brokerage_it_is_charged_on() -> None:
    """GST is 18% of brokerage plus exchange charge, so it must track the fix."""
    ctrl = controls()
    capped = transaction_costs(Decimal("1000"), 1000, "BUY", ctrl, Decimal("0"))
    beyond = transaction_costs(Decimal("1000"), 1000, "BUY", ctrl, Decimal("1000000"))
    assert beyond.gst < capped.gst


# --- what this is worth at the two account sizes -------------------------


def test_the_overcharge_happens_at_large_fills_not_small_ones() -> None:
    """Where the error actually bit, which is the opposite of the obvious guess.

    Below the cap brokerage is linear, so splitting an order changes nothing: a
    small position filled in eleven slices was always charged correctly. The
    error appears only when an individual fill is large enough to reach the cap
    on its own, because the old code then charged the whole cap for each slice.
    A 26 lakh position filling in eleven slices of 2.4 lakh paid the cap eleven
    times.

    It is worth fixing and worth being clear about: against a 10,000 risk budget
    the overcharge is 0.02 R, which changes no conclusion. The 0.42 R cost of
    trading a 10,000 account is unaffected by it.
    """
    ctrl = controls()
    slices = [100] * 11

    def charged_per_fill(price: Decimal) -> Decimal:
        return sum(transaction_costs(price, q, "BUY", ctrl, Decimal("0")).brokerage for q in slices)

    # Small fills, 2,600 each: 0.03% is 0.78, nowhere near the 20 cap.
    small_price = Decimal("26")
    assert charged_per_fill(small_price) == total_across_fills(small_price, slices, "BUY", ctrl)

    # Large fills, 242,000 each: 0.03% is 72.60, so each slice hit the cap.
    large_price = Decimal("2420")
    assert total_across_fills(large_price, slices, "BUY", ctrl) == Decimal("20")
    assert charged_per_fill(large_price) == Decimal("220")

    # Against the 10,000 risk budget that position size implies, 0.02 R.
    overcharge_in_r = (charged_per_fill(large_price) - Decimal("20")) / Decimal("10000")
    assert overcharge_in_r < Decimal("0.03")
