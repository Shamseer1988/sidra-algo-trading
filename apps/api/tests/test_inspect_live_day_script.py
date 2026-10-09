"""The limit a broker holds our order at, when we did not set one.

Every protective stop goes out as SL-M and every exit as MARKET, both without
a price. Upstox answers with a limit anyway. On 9 October:

  stop sent SL-M   trigger 1,248.60   book says limit 1,236.10   a 1.00% band
  exit sent MARKET no price           book says limit 1,249.80   a 1.01% band
                                      and it filled at 1,262.50

Both bound what the order can do. A stop that gaps more than 1% past its
trigger does not fill and the position stays open with the loss running; a
square-off into a market falling more than 1% does not fill either, and an
intraday position is left to the broker's own auto-square-off.

The earlier version of this only looked at orders carrying a trigger, so it
said nothing at all about the exit -- the half of the pair with no second
chance.
"""

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspect_live_day  # noqa: E402


def order(**kwargs) -> SimpleNamespace:
    base = {"broker_order_id": "1", "trigger_price": None, "limit_price": None, "average_price": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_a_limit_we_chose_ourselves_is_not_a_finding() -> None:
    """The entry goes out as a priced LIMIT. Reporting our own number back as
    the broker's would make every row look like a finding."""
    entry = order(limit_price=Decimal("1259.60"), average_price=Decimal("1258.90"))
    assert inspect_live_day.order_shape(entry, False) is None


def test_the_stop_reports_its_band_against_the_trigger() -> None:
    note = inspect_live_day.order_shape(order(trigger_price=Decimal("1248.60"), limit_price=Decimal("1236.10")), True)
    assert "1.00%" in note
    assert "1,236.10" in note and "1,248.60" in note


def test_a_market_exit_reports_its_band_against_the_fill() -> None:
    """It carries no trigger, so the fill is what the band is measured from.
    This is the case the trigger-only version could not see."""
    note = inspect_live_day.order_shape(order(limit_price=Decimal("1249.80"), average_price=Decimal("1262.50")), True)
    assert note is not None
    assert "1.01%" in note


def test_a_stop_resting_on_its_own_trigger_will_not_fill_past_it() -> None:
    note = inspect_live_day.order_shape(order(trigger_price=Decimal("1239.70"), limit_price=Decimal("1239.70")), True)
    assert "will not fill past it" in note


def test_a_zero_limit_is_a_true_market_order() -> None:
    note = inspect_live_day.order_shape(order(trigger_price=Decimal("194.63"), limit_price=Decimal("0")), True)
    assert "whatever the book offers" in note


def test_a_limit_the_broker_did_not_report_is_not_read_as_zero() -> None:
    """None is silence and zero is unprotected. Collapsing them would report
    an unknown order as a safe one."""
    note = inspect_live_day.order_shape(order(trigger_price=Decimal("194.63")), True)
    assert "did not report" in note


def test_a_limit_with_nothing_to_measure_against_still_says_so() -> None:
    """No trigger and no fill yet -- a resting market order. The limit is
    still the broker's, and still worth naming."""
    note = inspect_live_day.order_shape(order(limit_price=Decimal("1249.80")), True)
    assert "we did not set" in note
    assert "1,249.80" in note


def test_every_shape_is_a_distinct_sentence_an_operator_can_act_on() -> None:
    limits = (None, Decimal("0"), Decimal("194.63"), Decimal("190.00"))
    notes = [
        inspect_live_day.order_shape(order(trigger_price=Decimal("194.63"), limit_price=it), True) for it in limits
    ]
    assert all(note and len(note) > 20 for note in notes)
    assert len(set(notes)) == len(notes)
