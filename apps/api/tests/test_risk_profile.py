"""What the risk settings permit once they are read together.

Six controls interact, and set independently they contradicted each other
silently: 1% of ₹10,000 is ₹100 of planned risk against a 2% daily budget of
₹200, so the third and fourth trades were refused however high the trade ceiling
was set. The operator read "maximum 4 trades", got 2, and nothing said why.

These tests protect the answer to that, which is not a validator. Refusing the
combination would also refuse legitimate ones — a deliberately tight budget
under a generous ceiling is a reasonable thing to want, and no rule can tell it
apart from a mistake. So the effective capacity is derived, the binding control
is named, and the contradiction is reported.
"""

from decimal import Decimal

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls
from app.services.risk_profile import (
    CAUTIOUS_PAPER_START,
    RISK_PRESETS,
    USER_ADVANCED,
    effective_limits,
)


def controls(**overrides) -> dict:
    return {**DEFAULT_TRADING_CONTROLS, **overrides}


# --- the contradiction that started this ---------------------------------


def test_the_old_defaults_allowed_two_trades_while_advertising_four() -> None:
    """The exact configuration that shipped, kept as a regression case."""
    limits = effective_limits(
        controls(risk_per_trade_percent=1.0, maximum_daily_risk_percent=2.0, maximum_daily_trades=4)
    )
    assert limits.planned_risk_per_trade == Decimal("100.00")
    assert limits.daily_risk_budget == Decimal("200.00")
    assert limits.configured_trade_ceiling == 4
    assert limits.effective_trade_ceiling == 2
    assert limits.binding_control == "maximum_daily_risk_percent"


def test_the_contradiction_is_reported_with_the_fix() -> None:
    """An operator should not have to do this arithmetic to find out."""
    limits = effective_limits(
        controls(risk_per_trade_percent=1.0, maximum_daily_risk_percent=2.0, maximum_daily_trades=4)
    )
    warning = next(w for w in limits.warnings if "configured trades" in w)
    assert "only 2 of the 4" in warning
    assert "4.00%" in warning


def test_the_shipped_defaults_no_longer_contradict_themselves() -> None:
    limits = effective_limits(DEFAULT_TRADING_CONTROLS)
    assert limits.effective_trade_ceiling == limits.configured_trade_ceiling == 4
    assert limits.binding_control == "maximum_daily_trades"
    assert not [w for w in limits.warnings if "configured trades" in w]


# --- the binding control --------------------------------------------------


def test_a_deliberately_tight_budget_is_reported_not_refused() -> None:
    """Nothing here rejects a combination; it explains one."""
    limits = effective_limits(controls(maximum_daily_risk_percent=1.0, maximum_daily_trades=4))
    assert limits.effective_trade_ceiling == 1
    assert limits.binding_control == "maximum_daily_risk_percent"


def test_the_trade_ceiling_binds_when_the_budget_is_generous() -> None:
    limits = effective_limits(controls(maximum_daily_risk_percent=10.0, maximum_daily_trades=4))
    assert limits.effective_trade_ceiling == 4
    assert limits.binding_control == "maximum_daily_trades"


def test_a_budget_that_funds_two_and_a_half_trades_funds_two() -> None:
    """Whole trades only. Half a trade is not a trade."""
    limits = effective_limits(controls(risk_per_trade_percent=1.0, maximum_daily_risk_percent=2.5))
    assert limits.trades_the_budget_allows == 2


# --- exposure is not cash -------------------------------------------------


def test_exposure_is_capital_times_percent_times_leverage() -> None:
    """The label reads as a percent of capital and is not. 100% at 5x on
    ₹10,000 of cash is ₹50,000 of exposure — the ceiling Shamseer specified."""
    limits = effective_limits(
        controls(
            account_capital=10000.0,
            maximum_open_exposure_percent=100.0,
            intraday_leverage_enabled=True,
            intraday_leverage_multiplier=5.0,
        )
    )
    assert limits.exposure_ceiling == Decimal("50000.00")
    assert limits.leverage_multiplier == Decimal("5.0")


def test_leverage_off_means_exposure_is_capped_at_capital() -> None:
    limits = effective_limits(
        controls(maximum_open_exposure_percent=100.0, intraday_leverage_enabled=False, intraday_leverage_multiplier=5.0)
    )
    assert limits.exposure_ceiling == Decimal("10000.00")


# --- the implied percentage -----------------------------------------------


def test_the_loss_stop_reports_what_fraction_of_capital_it_is() -> None:
    """₹1,000 on ₹10,000 is 10%, and the operator should see that without
    reaching for a calculator when they edit either number."""
    limits = effective_limits(controls(account_capital=10000.0, daily_loss_limit=1000.0))
    assert limits.daily_loss_percent == Decimal("10.00")


def test_the_percentage_follows_an_edited_capital() -> None:
    limits = effective_limits(controls(account_capital=25000.0, daily_loss_limit=1000.0))
    assert limits.daily_loss_percent == Decimal("4.00")


def test_zero_capital_reports_no_percentage_rather_than_dividing_by_it() -> None:
    limits = effective_limits(controls(account_capital=0.0, daily_loss_limit=1000.0))
    assert limits.daily_loss_percent is None


# --- warnings worth reading ----------------------------------------------


def test_a_loss_stop_smaller_than_one_trade_says_so() -> None:
    """One losing trade ends the day. That is a choice, not a bug — but it
    should be a choice somebody made knowingly."""
    limits = effective_limits(controls(risk_per_trade_percent=2.5, daily_loss_limit=100.0))
    assert any("single losing trade ends the day" in w for w in limits.warnings)


def test_no_loss_stop_is_called_out() -> None:
    assert any("no money-based floor" in w for w in effective_limits(controls(daily_loss_limit=0.0)).warnings)


def test_several_concurrent_positions_are_called_out() -> None:
    limits = effective_limits(controls(maximum_open_positions=4))
    assert any("more than one stop can be hit" in w for w in limits.warnings)


# --- the two presets ------------------------------------------------------


def test_both_presets_are_internally_consistent() -> None:
    """A preset that contradicted itself would be the original bug, shipped
    under a friendlier name."""
    for key, preset in RISK_PRESETS.items():
        limits = effective_limits({**DEFAULT_TRADING_CONTROLS, **preset["controls"]})
        assert limits.effective_trade_ceiling == 4, key
        assert limits.binding_control == "maximum_daily_trades", key
        assert not [w for w in limits.warnings if "configured trades" in w], key


def test_the_cautious_preset_matches_the_specification() -> None:
    limits = effective_limits({**DEFAULT_TRADING_CONTROLS, **RISK_PRESETS[CAUTIOUS_PAPER_START]["controls"]})
    assert limits.planned_risk_per_trade == Decimal("100.00")
    assert limits.daily_loss_limit == Decimal("400.0")
    assert limits.maximum_open_positions == 1


def test_the_advanced_preset_matches_the_specification() -> None:
    limits = effective_limits({**DEFAULT_TRADING_CONTROLS, **RISK_PRESETS[USER_ADVANCED]["controls"]})
    assert limits.planned_risk_per_trade == Decimal("250.00")
    assert limits.daily_loss_limit == Decimal("1000.0")
    assert limits.daily_loss_percent == Decimal("10.00")
    assert limits.maximum_open_positions == 1


def test_both_presets_stop_the_day_at_two_thousand() -> None:
    for key, preset in RISK_PRESETS.items():
        assert preset["controls"]["daily_profit_target"] == 2000.0, key


@pytest.mark.parametrize("key", list(RISK_PRESETS))
def test_every_preset_validates_against_the_controls_schema(key: str) -> None:
    """A preset that could not be saved would be a button that always fails."""
    TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, **RISK_PRESETS[key]["controls"]})


# --- shape ----------------------------------------------------------------


def test_the_model_and_a_plain_dict_are_read_the_same_way() -> None:
    """The settings route passes a model; a stored row is a dict. They must not
    disagree about what the account is allowed to do."""
    as_dict = effective_limits(DEFAULT_TRADING_CONTROLS).snapshot()
    as_model = effective_limits(TradingControls.model_validate(DEFAULT_TRADING_CONTROLS)).snapshot()
    assert as_dict == as_model


def test_the_snapshot_is_json_serialisable_for_the_audit_record() -> None:
    import json

    assert json.loads(json.dumps(effective_limits(DEFAULT_TRADING_CONTROLS).snapshot()))["binding_control"]


def test_unreadable_values_do_not_raise() -> None:
    """This runs inside a settings read; it must not be able to break one."""
    limits = effective_limits({"account_capital": "nonsense", "risk_per_trade_percent": None})
    assert limits.planned_risk_per_trade == Decimal("0.00")
