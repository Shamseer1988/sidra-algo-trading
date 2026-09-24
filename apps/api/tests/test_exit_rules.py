"""Per-strategy exit rules, and the promise that turning them on changes nothing.

The first test in this file is the contract the whole change rests on: with
every field left at its default, the stop and the target are exactly what the
hard-coded arithmetic produced before. A strategy stored before these fields
existed has to keep trading the way it was measured, or every past result
silently describes a system that no longer exists.

After that, the two rules that are genuinely new. Both are off by default —
nothing here ever moved a stop or closed a position on the clock — and both are
guarded by invariants that matter more than the feature: a stop that can widen
increases risk on a trade already taken, and a stop placed through the current
price is a fill at a price nobody chose.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import exit_rules
from app.services.exit_rules import (
    ATR_MULTIPLE,
    ATR_TRAIL,
    BREAKEVEN_AT_R,
    DEFAULT_EXIT_RULES,
    NO_TRAIL,
    RR_MULTIPLE,
    ExitRules,
    from_controls,
    plan,
    time_exit_due,
    trail_to,
)

ENTRY = Decimal("100")
STRUCTURAL = Decimal("98")
ATR = Decimal("1")


def old_arithmetic(side, entry, structural_stop, atr, stop_atr_multiple, min_stop_percent, minimum_rr):
    """The computation that used to live inline in paper_strategy._stop_target_quantity.

    Copied verbatim rather than imported, so that the test fails if the new
    module drifts from it — importing the new code to check the new code would
    prove nothing.
    """
    structural_distance = (entry - structural_stop) if side == "LONG" else (structural_stop - entry)
    atr_floor = atr * Decimal(str(stop_atr_multiple)) if atr is not None and atr > 0 else Decimal("0")
    percent_floor = entry * Decimal(str(min_stop_percent)) / Decimal("100")
    risk_per_unit = max(structural_distance, atr_floor, percent_floor)
    if side == "LONG":
        return entry - risk_per_unit, entry + (risk_per_unit * Decimal(str(minimum_rr)))
    return entry + risk_per_unit, entry - (risk_per_unit * Decimal(str(minimum_rr)))


# --- the contract --------------------------------------------------------


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("atr_multiple", [0, 1.0, 1.5, 3.0])
@pytest.mark.parametrize("percent", [0, 0.4, 2.0])
@pytest.mark.parametrize("rr", [1.5, 2.0, 3.0])
def test_the_defaults_reproduce_the_old_arithmetic_exactly(side, atr_multiple, percent, rr):
    structural = STRUCTURAL if side == "LONG" else Decimal("102")
    result = plan(
        side=side,
        entry=ENTRY,
        structural_stop=structural,
        atr=ATR,
        rules=DEFAULT_EXIT_RULES,
        account_stop_atr_multiple=atr_multiple,
        account_min_stop_percent=percent,
        minimum_rr=rr,
    )
    expected_stop, expected_target = old_arithmetic(side, ENTRY, structural, ATR, atr_multiple, percent, rr)
    assert result is not None
    assert (result.stop, result.target) == (expected_stop, expected_target)


def test_the_defaults_do_not_trail():
    assert DEFAULT_EXIT_RULES.trailing_rule == NO_TRAIL
    assert (
        trail_to(
            side="LONG",
            entry=ENTRY,
            current_stop=Decimal("98"),
            risk_per_unit=Decimal("2"),
            candle_close=Decimal("120"),
            candle_extreme=Decimal("125"),
            atr=ATR,
            rules=DEFAULT_EXIT_RULES,
        )
        is None
    )


def test_the_defaults_never_close_on_the_clock():
    assert DEFAULT_EXIT_RULES.time_exit_minutes is None
    assert DEFAULT_EXIT_RULES.square_off_time is None
    assert (
        time_exit_due(
            opened_at=datetime(2026, 9, 24, 3, 45, tzinfo=UTC),
            now=datetime(2026, 9, 24, 23, 0, tzinfo=UTC),
            rules=DEFAULT_EXIT_RULES,
        )
        is None
    )


# --- stop and target -----------------------------------------------------


def test_the_widest_candidate_wins():
    # 4% of 100 is 4, which beats both the 2-point structure and the 1.5 ATR.
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=ATR,
        rules=DEFAULT_EXIT_RULES,
        account_stop_atr_multiple=1.5,
        account_min_stop_percent=4.0,
        minimum_rr=1.5,
    )
    assert result.risk_per_unit == Decimal("4")


def test_a_strategy_can_override_the_account_atr_multiple():
    rules = ExitRules(stop_atr_multiple=5.0)
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=ATR,
        rules=rules,
        account_stop_atr_multiple=1.0,
        account_min_stop_percent=0,
        minimum_rr=1.5,
    )
    assert result.risk_per_unit == Decimal("5")


def test_leaving_the_override_blank_still_follows_the_account():
    assert DEFAULT_EXIT_RULES.stop_atr_multiple is None
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=ATR,
        rules=DEFAULT_EXIT_RULES,
        account_stop_atr_multiple=3.0,
        account_min_stop_percent=0,
        minimum_rr=1.5,
    )
    assert result.risk_per_unit == Decimal("3")


def test_an_atr_target_is_measured_from_entry_not_from_risk():
    rules = ExitRules(target_rule=ATR_MULTIPLE, target_atr_multiple=4.0)
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=ATR,
        rules=rules,
        account_stop_atr_multiple=0,
        account_min_stop_percent=0,
        minimum_rr=1.5,
    )
    assert result.target == Decimal("104")
    assert result.stop == Decimal("98")


def test_an_atr_target_falls_back_to_reward_risk_when_atr_is_missing():
    # A target computed from a missing ATR would be a target at the entry price,
    # which is a guaranteed scratch dressed up as a plan.
    rules = ExitRules(target_rule=ATR_MULTIPLE, target_atr_multiple=4.0)
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=None,
        rules=rules,
        account_stop_atr_multiple=0,
        account_min_stop_percent=0,
        minimum_rr=2.0,
    )
    assert result.target == Decimal("104")  # 2 points of risk at 2R


def test_a_strategy_target_rr_overrides_the_minimum():
    rules = ExitRules(target_rr=5.0)
    result = plan(
        side="LONG",
        entry=ENTRY,
        structural_stop=STRUCTURAL,
        atr=None,
        rules=rules,
        account_stop_atr_multiple=0,
        account_min_stop_percent=0,
        minimum_rr=1.5,
    )
    assert result.target == Decimal("110")


def test_a_zero_width_stop_is_refused():
    assert (
        plan(
            side="LONG",
            entry=ENTRY,
            structural_stop=ENTRY,
            atr=None,
            rules=DEFAULT_EXIT_RULES,
            account_stop_atr_multiple=0,
            account_min_stop_percent=0,
            minimum_rr=1.5,
        )
        is None
    )


# --- trailing ------------------------------------------------------------


def test_breakeven_does_not_move_before_the_trigger():
    rules = ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=1.0)
    assert (
        trail_to(
            side="LONG",
            entry=ENTRY,
            current_stop=Decimal("98"),
            risk_per_unit=Decimal("2"),
            candle_close=Decimal("101.5"),
            candle_extreme=Decimal("101.9"),
            atr=ATR,
            rules=rules,
        )
        is None
    )


def test_breakeven_moves_the_stop_to_entry_once_ahead():
    rules = ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=1.0)
    moved = trail_to(
        side="LONG",
        entry=ENTRY,
        current_stop=Decimal("98"),
        risk_per_unit=Decimal("2"),
        candle_close=Decimal("103"),
        candle_extreme=Decimal("104"),
        atr=ATR,
        rules=rules,
    )
    assert moved == ENTRY


def test_a_stop_never_widens():
    # The invariant that makes trailing safe rather than dangerous: a rule that
    # could move a stop away from price would increase the risk of a trade
    # already taken.
    rules = ExitRules(trailing_rule=ATR_TRAIL, trailing_atr_multiple=10.0)
    assert (
        trail_to(
            side="LONG",
            entry=ENTRY,
            current_stop=Decimal("99"),
            risk_per_unit=Decimal("1"),
            candle_close=Decimal("101"),
            candle_extreme=Decimal("101"),
            atr=ATR,
            rules=rules,
        )
        is None
    )


def test_a_stop_is_never_placed_through_the_current_price():
    # 105 extreme minus 0.5 ATR would put the stop at 104.5, above a close of
    # 104 — filled on the next tick at a price nobody chose.
    rules = ExitRules(trailing_rule=ATR_TRAIL, trailing_atr_multiple=0.5)
    assert (
        trail_to(
            side="LONG",
            entry=ENTRY,
            current_stop=Decimal("98"),
            risk_per_unit=Decimal("2"),
            candle_close=Decimal("104"),
            candle_extreme=Decimal("105"),
            atr=ATR,
            rules=rules,
        )
        is None
    )


def test_an_atr_trail_follows_the_high_for_a_long():
    # High 112, two ATR back is 110, which is under the 111.80 close — so the
    # stop moves there. The ordinary case: a candle that closes near its high.
    rules = ExitRules(trailing_rule=ATR_TRAIL, trailing_atr_multiple=2.0)
    moved = trail_to(
        side="LONG",
        entry=ENTRY,
        current_stop=Decimal("98"),
        risk_per_unit=Decimal("2"),
        candle_close=Decimal("111.80"),
        candle_extreme=Decimal("112"),
        atr=ATR,
        rules=rules,
    )
    assert moved == Decimal("110")


def test_an_atr_trail_follows_the_low_for_a_short():
    rules = ExitRules(trailing_rule=ATR_TRAIL, trailing_atr_multiple=2.0)
    moved = trail_to(
        side="SHORT",
        entry=ENTRY,
        current_stop=Decimal("102"),
        risk_per_unit=Decimal("2"),
        candle_close=Decimal("88.20"),
        candle_extreme=Decimal("88"),
        atr=ATR,
        rules=rules,
    )
    assert moved == Decimal("90")


def test_an_atr_trail_without_atr_does_nothing():
    rules = ExitRules(trailing_rule=ATR_TRAIL)
    assert (
        trail_to(
            side="LONG",
            entry=ENTRY,
            current_stop=Decimal("98"),
            risk_per_unit=Decimal("2"),
            candle_close=Decimal("110"),
            candle_extreme=Decimal("112"),
            atr=None,
            rules=rules,
        )
        is None
    )


# --- the clock -----------------------------------------------------------

OPENED = datetime(2026, 9, 24, 4, 30, tzinfo=UTC)  # 10:00 IST


def test_a_holding_limit_closes_the_position_when_it_is_reached():
    rules = ExitRules(time_exit_minutes=45)
    assert time_exit_due(opened_at=OPENED, now=OPENED + timedelta(minutes=44), rules=rules) is None
    reason = time_exit_due(opened_at=OPENED, now=OPENED + timedelta(minutes=45), rules=rules)
    assert reason is not None and "45 minutes" in reason


def test_a_square_off_time_is_read_in_ist_not_utc():
    # 09:45 UTC is 15:15 IST. Reading the clock in UTC would square off five and
    # a half hours late, which on an intraday system is the next session.
    rules = ExitRules(square_off_time="15:15")
    assert time_exit_due(opened_at=OPENED, now=datetime(2026, 9, 24, 9, 44, tzinfo=UTC), rules=rules) is None
    reason = time_exit_due(opened_at=OPENED, now=datetime(2026, 9, 24, 9, 45, tzinfo=UTC), rules=rules)
    assert reason is not None and "15:15" in reason


def test_whichever_limit_arrives_first_wins():
    rules = ExitRules(time_exit_minutes=30, square_off_time="15:15")
    reason = time_exit_due(opened_at=OPENED, now=OPENED + timedelta(minutes=30), rules=rules)
    assert "30 minutes" in reason


def test_a_position_that_never_opened_has_no_clock():
    assert time_exit_due(opened_at=None, now=OPENED, rules=ExitRules(time_exit_minutes=1)) is None


def test_a_square_off_time_must_be_a_time():
    with pytest.raises(ValueError):
        ExitRules(square_off_time="quarter past three")
    with pytest.raises(ValueError):
        ExitRules(square_off_time="25:00")


# --- reading the rules off a signal --------------------------------------


def test_controls_without_rules_fall_back_to_the_defaults():
    assert from_controls({}) == DEFAULT_EXIT_RULES
    assert from_controls({"exit_rules": None}) == DEFAULT_EXIT_RULES


def test_unreadable_stored_rules_fall_back_rather_than_raise():
    # Execution refusing to manage an open position because its stored rules no
    # longer validate would be far worse than managing it the old way.
    assert from_controls({"exit_rules": {"trailing_rule": "SOMETHING_RENAMED"}}) == DEFAULT_EXIT_RULES


def test_stored_rules_are_read_back():
    stored = ExitRules(trailing_rule=BREAKEVEN_AT_R, time_exit_minutes=60).model_dump()
    assert from_controls({"exit_rules": stored}).time_exit_minutes == 60


def test_every_rule_has_a_label_for_the_screen():
    assert set(exit_rules.STOP_RULE_LABELS) == {exit_rules.WIDEST_OF}
    assert set(exit_rules.TARGET_RULE_LABELS) == {RR_MULTIPLE, ATR_MULTIPLE}
    assert set(exit_rules.TRAILING_LABELS) == {NO_TRAIL, BREAKEVEN_AT_R, ATR_TRAIL}
