"""Where a trade left, against where it was told to leave.

The live record says where every trade ended and nothing said why. The exit
manager computes a reason -- "Target reached at 412.80", "Square-off at 15:00
IST" -- and writes it to Telegram and to the log, never to a row, so no report
could group by it.

That gap hid the largest finding in the first fifteen live trades. Measured back
to gross, the average winner made 1.24R against a 2.00R target and the average
loser lost 0.66R against a 1.00R stop. The reward:risk ratio survived almost
intact, 1.89:1 against 2.0:1, so nothing on any screen looked wrong -- but a
fixed per-trade charge is a larger share of a smaller R, and the win rate the
account needed rose from 37.5% to 41.3% because of it. The trades were being cut
short and the only symptom was a break-even figure nobody could explain.

So the shape of an exit is derived here from what is recorded: the fill, the
planned levels, and the clock. It is inference, and these tests pin where the
inference is allowed to stop.
"""

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4

from app.services import exit_quality as quality
from app.services.exit_quality import EARLY, SQUARE_OFF, STOP, TARGET, UNKNOWN
from app.services.trade_history import BROKER, LIVE, TradeRecord
from app.services.trading_calendar import MARKET_TIMEZONE

SESSION = date(2026, 10, 16)
DEADLINE = time(15, 0)


def at_ist(hour: int, minute: int) -> datetime:
    """An instant that reads as this IST wall-clock time, which is the clock the
    square-off is expressed in and the only one the verdict may be judged on."""
    return datetime(2026, 10, 16, hour, minute, tzinfo=MARKET_TIMEZONE)


def trade(
    *,
    entry="400",
    stop="396",
    target="408",
    exit_price="404",
    side="LONG",
    closed_hour=11,
    closed_minute=30,
    open_quantity=0,
):
    """One trade. A 4.00 risk and an 8.00 reward, so the plan is exactly 2:1."""
    return TradeRecord(
        position_id=uuid4(),
        signal_id=uuid4(),
        session_date=SESSION,
        instrument_token="NSE_EQ|INE245A01021",
        script_name="TATAPOWER",
        side=side,
        strategy_version="orb-retest-v1@15",
        status="OPEN" if open_quantity else "CLOSED",
        execution_mode=LIVE,
        quantity=15,
        open_quantity=open_quantity,
        entry_price=None if entry is None else Decimal(entry),
        exit_price=None if exit_price is None else Decimal(exit_price),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        opened_at=at_ist(10, 0),
        closed_at=None if closed_hour is None else at_ist(closed_hour, closed_minute),
        gross_pnl=Decimal("60"),
        charges=Decimal("6"),
        net_pnl=Decimal("54"),
        unrealized_pnl=Decimal("0"),
        risk_amount=Decimal("60"),
        reconciliation="MATCHED",
        reconciliation_note="",
        broker="UPSTOX",
        price_source=BROKER,
        modelled_gross_pnl=Decimal("60"),
    )


def closed_at_ist(hour: int, minute: int, **overrides):
    return replace(trade(**overrides), closed_at=at_ist(hour, minute))


def shape(**overrides):
    return quality.shape(trade(**overrides), square_off=DEADLINE)


# --- R, measured against the plan the trade was given ----------------------


def test_the_plan_is_read_from_the_levels_not_assumed():
    """A 4.00 stop and an 8.00 target is 2:1 whatever the account's setting says.
    Reading minimum_rr instead would describe the setting, not the trade."""
    assert shape().planned_rr == Decimal("2.00")


def test_a_trade_that_took_half_its_target_says_half():
    """The figure the whole module exists for. +4.00 on a 4.00 risk is 1R, and
    1R of a 2R plan is 50% of the reward it was sized for."""
    result = shape(exit_price="404")
    assert result.gross_r == Decimal("1.00")
    assert result.reach == Decimal("0.50")
    assert result.verdict == EARLY


def test_reaching_the_target_is_named_as_reaching_it():
    result = shape(exit_price="408")
    assert result.gross_r == Decimal("2.00")
    assert result.reach == Decimal("1.00")
    assert result.verdict == TARGET


def test_a_fill_a_tick_short_of_the_target_still_counts_as_the_target():
    """A limit fills a tick or two short. Demanding the exact figure would file
    most real exits under 'left early' and bury the thing this is for."""
    assert shape(exit_price="407.95").verdict == TARGET


def test_a_live_market_exit_near_the_target_is_the_target():
    """The 16 October JSWSTEEL exit, in this fixture's numbers. A live target
    exit is not a resting limit: the manager watches for the close to cross the
    level and then sends a MARKET order, so the fill lands near the target
    rather than on it. That one came in 0.05R short of a 1.91R plan -- a good
    fill -- and a tighter test called it "left early", which is the one verdict
    that has to mean something was cutting trades short.
    """
    # 2R plan, exiting 0.05R short of it: 8.00 reward less 0.20.
    result = shape(exit_price="407.80")
    assert result.gross_r == Decimal("1.95")
    assert result.verdict == TARGET


def test_a_trade_that_gave_up_a_third_of_its_target_is_not_the_target():
    """The margin must stay narrow enough to leave the case this exists to
    find. Two thirds of a 2R target is 0.7R short, and no tick allowance
    reaches that."""
    result = shape(exit_price="405.32")
    assert result.gross_r == Decimal("1.33")
    assert result.reach == Decimal("0.66")
    assert result.verdict == EARLY


def test_a_stop_a_tenth_short_of_its_trigger_is_still_the_stop():
    """A stop that slipped the other way -- filled just before the level, which
    a gap through it can do -- is the stop, not an early exit."""
    assert shape(exit_price="396.40").verdict == STOP


def test_the_stop_is_named_even_when_it_slipped_past():
    """A stop fills past its trigger in a fast market; -1.06R is the stop."""
    assert shape(exit_price="395.75").verdict == STOP
    assert shape(exit_price="396").verdict == STOP


def test_a_short_is_measured_in_its_own_direction():
    """Price falling is a gain. Reusing the long arithmetic would report every
    winning short as a loss."""
    result = quality.shape(
        trade(side="SHORT", entry="400", stop="404", target="392", exit_price="392"), square_off=DEADLINE
    )
    assert result.gross_r == Decimal("2.00")
    assert result.verdict == TARGET


# --- the clock, only once the levels have been ruled out --------------------


def test_an_exit_at_the_deadline_short_of_both_levels_is_the_deadline():
    result = quality.shape(closed_at_ist(15, 1, exit_price="402"), square_off=DEADLINE)
    assert result.verdict == SQUARE_OFF


def test_the_deadline_does_not_claim_a_trade_that_reached_its_target():
    """A trade that hit its target at 14:58 reached its target. The deadline two
    minutes later did not close it, and attributing it there would hide a
    working target behind a square-off count."""
    result = quality.shape(closed_at_ist(14, 58, exit_price="408"), square_off=DEADLINE)
    assert result.verdict == TARGET


def test_a_mid_morning_exit_short_of_both_levels_is_not_the_deadline():
    assert quality.shape(closed_at_ist(11, 15, exit_price="402"), square_off=DEADLINE).verdict == EARLY


def test_with_no_deadline_configured_nothing_is_attributed_to_one():
    assert quality.shape(closed_at_ist(15, 10, exit_price="402"), square_off=None).verdict == EARLY


# --- what cannot be measured is not guessed at -----------------------------


def test_a_trade_with_no_fill_recorded_is_unknown_not_zero():
    """A 0.00R would read as a trade that went nowhere, which is a finding. Not
    knowing is not a finding."""
    result = quality.shape(trade(entry=None), square_off=DEADLINE)
    assert result.verdict == UNKNOWN
    assert result.gross_r is None and result.reach is None


def test_an_entry_on_top_of_its_own_stop_has_no_r_to_measure():
    """Dividing by a zero risk would raise, or invent an infinity."""
    assert quality.shape(trade(entry="396", stop="396"), square_off=DEADLINE).verdict == UNKNOWN


# --- the summaries the daily report prints ---------------------------------


def shapes_for(*exits: str):
    return [shape(exit_price=price) for price in exits]


def test_the_tally_counts_each_way_out_and_leaves_out_the_empty_rows():
    counts = quality.tally(shapes_for("408", "404", "396"))
    assert counts == {TARGET: 1, STOP: 1, EARLY: 1}
    assert SQUARE_OFF not in counts


def test_collected_is_measured_over_winners_only():
    """A loser's reach is a fraction of a risk, not of a reward. Averaging the
    two would answer a question nobody asked."""
    items = shapes_for("408", "404", "396")
    # The winners took 100% and 50% of their target; the loss is left out.
    assert quality.average_reach(items) == Decimal("0.75")


def test_the_gross_averages_split_the_two_sides():
    items = shapes_for("408", "404", "396")
    assert quality.average_gross_r(items, wins=True) == Decimal("1.50")
    assert quality.average_gross_r(items, wins=False) == Decimal("-1.00")


def test_nothing_to_average_is_absent_rather_than_zero():
    assert quality.average_reach([]) is None
    assert quality.average_gross_r(shapes_for("396"), wins=True) is None
