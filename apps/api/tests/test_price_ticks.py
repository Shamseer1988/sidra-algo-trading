"""Prices the exchange will accept, rounded in the direction that is safe.

Written from a live failure. On 5 October a long in BAJFINANCE filled at
₹995.40 and its protective stop was rejected by Upstox:

    You've entered an invalid trigger price ... place an order with the price
    in multiples of the tick size as mentioned by the Exchange.

The trigger was ``977.9632`` -- the signal's structural stop, stored to four
decimal places, which nothing had ever asked to be a price anyone could trade
at. The position was left open with nothing behind it. That exact number is the
first test here.
"""

from decimal import Decimal

import pytest

from app.services.live_orders import LiveOrderRequest
from app.services.price_ticks import TICK, is_on_tick, round_to_tick

# Ragged prices of the kind a structural stop or an ATR multiple produces.
RAGGED = [
    Decimal("977.9632"),
    Decimal("185.2231"),
    Decimal("0.0100"),
    Decimal("1.0000"),
    Decimal("99999.9999"),
    Decimal("183.6300"),
    Decimal("1223.7412"),
]


def test_the_trigger_the_exchange_rejected():
    """The number from the live rejection, and what should have been sent."""
    assert round_to_tick(Decimal("977.9632"), "SELL") == Decimal("978.00")
    assert is_on_tick(Decimal("977.9632")) is False
    assert is_on_tick(Decimal("978.00")) is True


@pytest.mark.parametrize("price", RAGGED)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_every_rounded_price_is_one_the_exchange_accepts(price, side):
    assert is_on_tick(round_to_tick(price, side))


def test_buy_rounds_down_and_sell_rounds_up():
    assert round_to_tick(Decimal("100.03"), "BUY") == Decimal("100.00")
    assert round_to_tick(Decimal("100.03"), "SELL") == Decimal("100.05")


# --- the direction is a safety property, not a preference -----------------


@pytest.mark.parametrize("price", RAGGED)
def test_a_long_stop_never_moves_further_from_the_entry(price):
    """The protective stop on a long is a SELL below the entry.

    Rounding it *up* moves it toward the entry, so the rounding can only reduce
    what the trade can lose. Rounding to nearest would sometimes widen it, and
    a stop that quietly allows more loss than the budget is the thing this
    system exists to prevent.
    """
    assert round_to_tick(price, "SELL") >= price


@pytest.mark.parametrize("price", RAGGED)
def test_a_short_stop_never_moves_further_from_the_entry(price):
    """The mirror: a BUY stop above a short entry rounds down, toward it."""
    assert round_to_tick(price, "BUY") <= price


def test_a_limit_never_crosses_further_than_asked():
    """The same rule, read the other way: a BUY bids no more, a SELL offers no less."""
    assert round_to_tick(Decimal("995.44"), "BUY") <= Decimal("995.44")
    assert round_to_tick(Decimal("995.44"), "SELL") >= Decimal("995.44")


def test_rounding_moves_a_price_by_less_than_one_tick():
    """A correction larger than a tick would be a different trade."""
    for price in RAGGED:
        for side in ("BUY", "SELL"):
            assert abs(round_to_tick(price, side) - price) < TICK


# --- absence is not a price -----------------------------------------------


def test_a_market_order_carries_no_price_and_is_left_alone():
    assert round_to_tick(Decimal("0"), "BUY") == Decimal("0")
    assert round_to_tick(None, "SELL") is None


def test_a_price_already_on_the_grid_is_untouched():
    assert round_to_tick(Decimal("978.00"), "SELL") == Decimal("978.00")
    assert round_to_tick(Decimal("978.00"), "BUY") == Decimal("978.00")


def test_a_finer_tick_accepts_our_coarser_one():
    """Why one value is safe for every instrument in the segment.

    NSE uses ₹0.05, finer for low-priced securities. A multiple of ₹0.05 is
    also a multiple of ₹0.01, so the coarse grid is a subset of the fine one
    and this module needs no per-scrip data to be right.
    """
    for price in RAGGED:
        rounded = round_to_tick(price, "SELL")
        assert rounded % Decimal("0.01") == 0


# --- nothing can place an order off the grid ------------------------------


def request(side="SELL", price="0", trigger="977.9632", order_type="SL-M"):
    return LiveOrderRequest(
        instrument_token="NSE_EQ|INE296A01032",
        side=side,
        quantity=12,
        order_type=order_type,
        product="INTRADAY",
        price=Decimal(price),
        trigger_price=Decimal(trigger),
    )


def test_an_order_is_on_the_grid_from_birth():
    """Normalised in ``LiveOrderRequest`` rather than in its callers.

    This object is the only way an order can be expressed here, so a price
    corrected at construction is the same price in the write-ahead record, in
    the approval message the operator reads, and at the broker -- three places
    that had no way of agreeing before.
    """
    assert request().trigger_price == Decimal("978.00")


def test_the_broker_order_carries_the_corrected_price():
    assert request().to_broker_order("sidra-x").trigger_price == Decimal("978.00")


@pytest.mark.parametrize("price", RAGGED)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_no_live_order_can_carry_a_price_the_exchange_would_reject(price, side):
    """The guard. Every live order in this system is built from this class."""
    order = request(side=side, price=str(price), trigger=str(price), order_type="LIMIT").to_broker_order("sidra-x")
    assert is_on_tick(order.price), f"{side} limit {order.price} is off the tick grid"
    assert is_on_tick(order.trigger_price), f"{side} trigger {order.trigger_price} is off the tick grid"


def test_the_protective_stop_path_announces_what_it_sent():
    """The alert said "Stop at 977.9632" on a day the broker rejected that price.

    Scanned as code: the protection path has to put the stop on the grid before
    it is used, not leave the correction to the request and then announce the
    uncorrected number.
    """
    import io
    import tokenize
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "live_protection.py").read_text()
    code = " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    assert "round_to_tick" in code, "live_protection no longer rounds its stop before announcing it"
