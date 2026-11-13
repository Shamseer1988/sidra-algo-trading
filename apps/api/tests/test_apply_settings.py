"""Reading a setting off a command line, without changing what it means.

``apply_settings.py`` exists so a change can be written down, reviewed and
applied in one step. It is worth testing for one reason: the step between a
string typed on a terminal and a value handed to the model is where a settings
change goes quietly wrong. ``true`` must not be saved as the text "true", ``2``
must not become 2.0, ``14:45`` must survive as a time, and a dotted key must
reach inside the exit rules rather than adding a key called
"exit_rules.trailing_rule" that nothing reads — which is how a trailing rule
was once swept while every variant replayed identically.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import apply_settings as apply  # noqa: E402

# --- a string from a terminal, as the value it means ----------------------


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "on", "1"])
def test_the_words_for_yes_become_a_boolean(raw: str):
    """A leverage switch is not the number one. True == 1 in Python, and a
    settings diff that cannot tell them apart reports edits nobody made."""
    assert apply.parse_value(raw) is True


@pytest.mark.parametrize("raw", ["false", "no", "off", "0"])
def test_the_words_for_no_become_a_boolean(raw: str):
    assert apply.parse_value(raw) is False


def test_a_whole_number_stays_whole():
    """A trade ceiling of 3 is three trades, not 3.0 of something."""
    value = apply.parse_value("3")
    assert value == 3 and isinstance(value, int)


def test_a_decimal_stays_a_decimal():
    value = apply.parse_value("1.5")
    assert value == 1.5 and isinstance(value, float)


@pytest.mark.parametrize("raw", ["14:45", "BREAKEVEN_AT_R", "UPSTOX", "LIMIT"])
def test_text_the_models_need_as_text_survives(raw: str):
    """A time compared as text against each candle's IST clock, and a rule name
    the model matches literally. Coerced to anything else, both stop working
    without failing."""
    assert apply.parse_value(raw) == raw


def test_an_emptied_value_is_none_not_an_empty_string():
    """None means 'use the account control' on several strategy fields. An
    empty string is not a number and would be refused."""
    assert apply.parse_value("") is None
    assert apply.parse_value("none") is None


@pytest.mark.parametrize("key", ["trailing_rule", "exit_rules.trailing_rule"])
def test_the_trailing_rule_called_none_stays_the_name_of_a_rule(key: str):
    """ "NONE" is one of trailing_rule's three names, not the absence of one.

    Parsed as Python None it was refused as a NoneType where a literal was
    wanted, so the terminal could not switch off the one exit rule the sweeps
    had just argued for, and the refusal pointed at the type rather than the
    mistake. Both spellings of the key reach the same setting.
    """
    assert apply.split_pair(f"{key}=NONE") == (key, "NONE")


@pytest.mark.parametrize("key", ["exit_rules.stop_atr_multiple", "min_stop_distance_percent"])
def test_a_nullable_setting_still_empties_to_none(key: str):
    """The exception above is for one key only. Everywhere else the word is
    still how a strategy gives up an opinion and goes back to the account."""
    assert apply.split_pair(f"{key}=none") == (key, None)


def test_the_model_takes_the_parsed_trailing_rule():
    """The parse and the model have to agree, which only a round trip shows."""
    from app.services.strategy_registry import StrategyConfiguration

    payload = StrategyConfiguration(id="orb-retest-default", name="ORB Retest").model_dump()
    key, value = apply.split_pair("exit_rules.trailing_rule=NONE")
    apply.apply_path(payload, key, value)
    assert StrategyConfiguration.model_validate(payload).exit_rules.trailing_rule == "NONE"


def test_the_leaf_of_a_key_is_the_part_after_the_one_dot():
    assert apply.leaf("exit_rules.trailing_rule") == "trailing_rule"
    assert apply.leaf("trailing_rule") == "trailing_rule"
    assert apply.leaf("") == ""


def test_a_pair_splits_on_the_first_equals_only():
    assert apply.split_pair("trade_cutoff_time=14:45") == ("trade_cutoff_time", "14:45")


def test_a_pair_with_no_equals_is_refused():
    with pytest.raises(SystemExit):
        apply.split_pair("minimum_score")


# --- one dot reaches inside a nested block --------------------------------


def test_a_flat_key_is_set_flat():
    payload = {"minimum_score": 65}
    apply.apply_path(payload, "minimum_score", 75)
    assert payload == {"minimum_score": 75}


def test_a_dotted_key_reaches_inside_the_exit_rules():
    """The whole reason this function exists. A flat update would add a key
    nothing reads and the save would look successful."""
    payload = {"exit_rules": {"trailing_rule": "NONE", "trailing_trigger_r": 1.0}}
    apply.apply_path(payload, "exit_rules.trailing_rule", "BREAKEVEN_AT_R")
    assert payload["exit_rules"] == {"trailing_rule": "BREAKEVEN_AT_R", "trailing_trigger_r": 1.0}
    assert "exit_rules.trailing_rule" not in payload


def test_the_rest_of_the_nested_block_is_left_alone():
    payload = {"exit_rules": {"trailing_rule": "NONE", "square_off_time": "15:00", "target_atr_multiple": 2.0}}
    apply.apply_path(payload, "exit_rules.trailing_rule", "BREAKEVEN_AT_R")
    assert payload["exit_rules"]["square_off_time"] == "15:00"
    assert payload["exit_rules"]["target_atr_multiple"] == 2.0


def test_a_dotted_key_into_something_that_is_not_a_block_is_refused():
    with pytest.raises(SystemExit):
        apply.apply_path({"minimum_score": 65}, "minimum_score.inner", 1)


def test_the_diff_reads_the_leaf_not_the_block():
    """Printing forty characters of exit rules either side of a one-word change
    buries the word that moved."""
    payload = {"exit_rules": {"trailing_rule": "NONE"}, "minimum_score": 65}
    assert apply.read_path(payload, "exit_rules.trailing_rule") == "NONE"
    assert apply.read_path(payload, "minimum_score") == 65
    assert apply.read_path(payload, "exit_rules.absent") is None


# --- finding the strategy the operator meant ------------------------------


def strategy(identifier: str, kind: str, name: str):
    return SimpleNamespace(id=identifier, strategy_type=kind, name=name)


ORB = strategy("orb-retest-default", "orb-retest-v1", "ORB Retest — Default")
RS = strategy("rs-1", "rs-pullback-v1", "RS Pullback")


def test_a_typed_name_finds_the_strategy_however_it_is_punctuated():
    """The type is orb-retest-v1, the display name is "ORB Retest — Default",
    and the operator types orb-retest. Comparing raw strings matches neither."""
    assert apply.matches(ORB, "orb-retest")
    assert apply.matches(ORB, "ORB Retest")
    assert apply.matches(ORB, "orb-retest-v1")
    assert apply.matches(ORB, "orbretest")


def test_it_does_not_match_a_different_strategy():
    assert not apply.matches(RS, "orb-retest")
    assert not apply.matches(ORB, "rs-pullback")


def test_an_empty_selector_matches_nothing():
    """Otherwise a stray argument would change every strategy at once."""
    assert not apply.matches(ORB, "")
    assert not apply.matches(ORB, "   ")
