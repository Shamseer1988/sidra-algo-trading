"""The description a settings form is generated from.

The old screen rendered ``Object.entries(controls)`` as a grid of raw inputs
labelled by their snake_case key: "maximum open exposure percent" with a bare
number box, no unit, no range, and no hint that the number is multiplied by
leverage before it becomes rupees. An operator could not tell ₹10,000 from
₹50,000 without reading the risk engine.

The first two tests are the ones that keep this honest over time. Everything
else can be re-derived by reading; those two cannot be satisfied by care.
"""

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls
from app.services.settings_catalog import (
    EFFECT_LABELS,
    GROUP_LABELS,
    GROUP_ORDER,
    SPECS_BY_KEY,
    TRADING_CONTROL_SPECS,
)
from app.services.settings_history import summarise

# --- the two that cannot be satisfied by being careful -------------------


def test_every_control_is_described() -> None:
    """A control with no description would ship as a mystery box.

    This fails the build when somebody adds a field to TradingControls and
    forgets the prose, which is exactly when it would otherwise be forgotten.
    """
    assert set(SPECS_BY_KEY) == set(TradingControls.model_fields)


def test_no_description_outlives_its_control() -> None:
    """And a spec for a field that no longer exists would render a box that
    saves nothing."""
    assert set(SPECS_BY_KEY) <= set(TradingControls.model_fields)


# --- bounds come from the schema, not from the prose ---------------------


def test_bounds_are_read_from_the_schema_rather_than_restated() -> None:
    """Restating them here would eventually disagree with the validator, and
    the form would offer a range the server then refuses."""
    described = SPECS_BY_KEY["risk_per_trade_percent"].describe(TradingControls.model_fields, 1.0, None)
    assert described["exclusive_minimum"] == 0
    assert described["maximum"] == 5
    assert described["minimum"] is None


def test_an_integer_control_is_described_as_one() -> None:
    described = SPECS_BY_KEY["maximum_daily_trades"].describe(TradingControls.model_fields, 4, None)
    assert described["kind"] == "integer"
    assert described["minimum"] == 1


def test_a_choice_control_carries_its_options() -> None:
    """A free-text box for a value the server will refuse wastes an afternoon."""
    described = SPECS_BY_KEY["live_broker"].describe(TradingControls.model_fields, "NONE", None)
    assert described["kind"] == "choice"
    assert described["choices"] == ["NONE", "UPSTOX", "FIRSTOCK"]


def test_a_boolean_control_is_described_as_one() -> None:
    described = SPECS_BY_KEY["intraday_leverage_enabled"].describe(TradingControls.model_fields, True, None)
    assert described["kind"] == "boolean"


def test_a_time_control_is_described_as_one() -> None:
    described = SPECS_BY_KEY["trade_start_time"].describe(TradingControls.model_fields, "09:24", None)
    assert described["kind"] == "time"


@pytest.mark.parametrize("spec", TRADING_CONTROL_SPECS, ids=lambda spec: spec.key)
def test_every_choice_control_offers_choices_and_no_other_control_does(spec) -> None:
    assert bool(spec.choices) == (spec.unit == "CHOICE"), spec.key


# --- what the prose has to carry -----------------------------------------


@pytest.mark.parametrize("spec", TRADING_CONTROL_SPECS, ids=lambda spec: spec.key)
def test_every_control_has_a_unit_a_group_and_an_effect(spec) -> None:
    """A number with no unit is the problem this replaces."""
    assert spec.unit
    assert spec.group in GROUP_LABELS
    assert spec.effect in EFFECT_LABELS


@pytest.mark.parametrize("spec", TRADING_CONTROL_SPECS, ids=lambda spec: spec.key)
def test_every_control_explains_itself_rather_than_restating_its_name(spec) -> None:
    """ "Maximum daily trades: the maximum daily trades" helps nobody.

    Length is a crude proxy for substance, and deliberately the only one: a
    label like "Account capital" is the right words for that control, so
    asserting that labels differ from their keys would reject good prose.
    """
    assert len(spec.help) > 60, spec.key


def test_the_money_controls_are_labelled_in_rupees_not_percent() -> None:
    """A daily stop is an amount somebody is willing to lose, and must not
    quietly rescale when capital is edited."""
    assert SPECS_BY_KEY["daily_loss_limit"].unit == "INR"
    assert SPECS_BY_KEY["daily_profit_target"].unit == "INR"


def test_the_exposure_control_says_it_is_multiplied_by_leverage() -> None:
    """The label reads as a percent of capital and is not. This is the
    sentence that stops ₹50,000 being read as ₹50,000 of cash."""
    help_text = SPECS_BY_KEY["maximum_open_exposure_percent"].help
    assert "leverage" in help_text
    assert "never cash" in help_text


def test_the_loss_stop_does_not_promise_the_loss_stops_there() -> None:
    """A stop is an instruction, not a guarantee. Saying otherwise in a
    tooltip is how somebody sizes a position they cannot afford."""
    assert "cannot guarantee" in SPECS_BY_KEY["daily_loss_limit"].help


def test_the_score_control_warns_that_scores_are_not_comparable_with_old_ones() -> None:
    """Missing inputs used to score full marks; an operator tuning the
    threshold against last week's numbers would tune it against a bug."""
    assert "missing input" in SPECS_BY_KEY["minimum_score"].help.lower()


def test_every_group_is_used_and_ordered() -> None:
    used = {spec.group for spec in TRADING_CONTROL_SPECS}
    assert used == set(GROUP_ORDER) == set(GROUP_LABELS)
    assert len(GROUP_ORDER) == len(set(GROUP_ORDER))


# --- change detection -----------------------------------------------------


def test_only_the_keys_that_moved_are_reported() -> None:
    before = dict(DEFAULT_TRADING_CONTROLS)
    after = {**before, "minimum_score": 75}
    assert summarise(before, after).changed_keys == ["minimum_score"]


def test_the_same_number_in_two_shapes_is_not_a_change() -> None:
    """4 from a form and 4.0 from JSON are the same setting. Reporting it as
    a change would fill the history with edits nobody made."""
    before = {"maximum_daily_trades": 4}
    after = {"maximum_daily_trades": 4.0}
    assert summarise(before, after).changed_keys == []


def test_loosening_a_limit_is_named_and_tightening_is_not() -> None:
    before = dict(DEFAULT_TRADING_CONTROLS)
    loosened = summarise(before, {**before, "daily_loss_limit": 2000.0})
    tightened = summarise(before, {**before, "daily_loss_limit": 100.0})
    assert loosened.risk_increased == ["daily_loss_limit: 400.0 -> 2000.0"]
    assert tightened.risk_increased == []
    # Both are still recorded as changes; only the framing differs.
    assert tightened.changed_keys == ["daily_loss_limit"]


def test_turning_leverage_on_is_not_reported_as_a_raised_number() -> None:
    """True > False in Python. It is a change, but not a numeric increase, and
    reporting it as one would make the audit line read as nonsense."""
    before = {**DEFAULT_TRADING_CONTROLS, "intraday_leverage_enabled": False}
    after = {**before, "intraday_leverage_enabled": True}
    summary = summarise(before, after)
    assert summary.changed_keys == ["intraday_leverage_enabled"]
    assert summary.risk_increased == []
