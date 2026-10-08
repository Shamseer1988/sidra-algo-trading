"""What a protective stop is actually resting on, read rather than reasoned about.

Every stop this system places goes out as SL-M. On 8 October Upstox's order book
answered "SL" for the one still resting and "LIMIT" for the one that had
triggered, and the triggered one filled at exactly its trigger price. Three
things explain that equally well — a protected market order, a conversion to
stop-limit, or a nine-share fill that simply landed on the bid — and the three
have different consequences in a gap.

The limit price tells them apart, so the script says which it is in words rather
than leaving an operator to infer it from two numbers at six in the evening.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspect_live_day  # noqa: E402

from app.services.broker_adapter import BrokerOrderRecord  # noqa: E402


def stop(**kwargs) -> BrokerOrderRecord:
    return BrokerOrderRecord(broker_order_id="1", client_order_id=None, status="open", symbol="RVNL-EQ", **kwargs)


def test_an_order_with_no_trigger_is_not_a_stop_and_says_nothing() -> None:
    """An entry is not a stop. Annotating every row would make the one row that
    matters indistinguishable from the rest."""
    assert inspect_live_day.stop_shape(stop(limit_price=Decimal("193.10"))) is None


def test_a_stop_resting_on_its_own_trigger_will_not_fill_past_it() -> None:
    note = inspect_live_day.stop_shape(stop(trigger_price=Decimal("1239.70"), limit_price=Decimal("1239.70")))
    assert "will not fill past it" in note
    assert "1,239.70" in note


def test_a_stop_with_no_limit_fills_at_market() -> None:
    """Zero is the broker saying there is no limit, which is what SL-M asks for."""
    note = inspect_live_day.stop_shape(stop(trigger_price=Decimal("194.63"), limit_price=Decimal("0")))
    assert "fills at market" in note


def test_a_protected_stop_reports_the_width_of_its_band() -> None:
    note = inspect_live_day.stop_shape(stop(trigger_price=Decimal("1239.70"), limit_price=Decimal("1215.00")))
    assert "24.70" in note


def test_a_limit_the_broker_did_not_report_is_not_read_as_zero() -> None:
    """None is silence and zero is a market stop. Collapsing them would report
    an unknown stop as a safe one."""
    note = inspect_live_day.stop_shape(stop(trigger_price=Decimal("194.63")))
    assert "did not report" in note


def test_every_stop_shape_is_a_sentence_an_operator_can_act_on() -> None:
    limits = (None, Decimal("0"), Decimal("194.63"), Decimal("190.00"))
    notes = [inspect_live_day.stop_shape(stop(trigger_price=Decimal("194.63"), limit_price=it)) for it in limits]
    assert all(note and len(note) > 20 for note in notes)
    assert len(set(notes)) == len(notes)
