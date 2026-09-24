"""Absence must never outscore evidence.

The defect these tests exist for: every scoring section used to award full
credit when its input was missing, and zero when the input was present and
unfavourable. An instrument with no VWAP, no relative volume, no NIFTY regime
and no relative strength collected 60 of 100 points for free against a threshold
of 80 — and tied with an instrument where all four were perfect.

The reason it went unnoticed is written into the old docstring: "the
data-quality gate already blocks bad data". That gate watches the feed, not the
indicators. A healthy feed on a newly tracked instrument passes it with none of
these computed, which is exactly the situation this deployment is in — the
relative-volume baseline needs ten sessions of history and there are about eight.

So the tests below are ordered by what they protect: first that nothing scores
full marks for absence, then that a required input blocks rather than discounts.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, LONG_BREAKOUT, SIGNALLED, evaluate_orb_retest
from app.services.signal_inputs import (
    ATR,
    DEFAULT_REQUIRED_INPUTS,
    EMA,
    REGIME,
    RELATIVE_STRENGTH,
    RVOL,
    SUPPORTED_INPUTS,
    VWAP,
    available_inputs,
    missing_required,
    normalise_required,
)
from app.services.strategy_registry import StrategyConfiguration

CONTROLS = {
    "account_capital": 100_000.0,
    "risk_per_trade_percent": 0.5,
    "maximum_daily_risk_percent": 1.0,
    "maximum_daily_trades": 2,
    "minimum_score": 80,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
}

FULL = {
    "opening_range": {"high": 110.0, "low": 100.0, "complete": True},
    "atr": 1.0,
    "opening_range_atr": 1.0,
    "vwap": 105.0,
    "ema_fast": 111.0,
    "ema_slow": 106.0,
    "volume": {"relative_volume": 3.0},
    "relative_strength": {"relative_strength_percent": 0.4},
}
NIFTY = {"nifty_regime": {"regime": "BULLISH"}}


def candle(*, close: str, low: str, high: str) -> CompletedCandle:
    opened_at = datetime(2026, 8, 31, 4, 2, tzinfo=UTC)  # 09:32 IST
    return CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("110.5"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=500,
        tick_count=10,
    )


def retest(indicators: dict, nifty: dict = NIFTY, controls: dict = CONTROLS):
    return evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), indicators, nifty, controls, LONG_BREAKOUT
    )


# --- nothing scores full marks for absence -------------------------------


@pytest.mark.parametrize(
    ("removal", "section"),
    [
        (lambda d: {**d, "atr": None, "opening_range_atr": None}, "breakout_retest"),
        (lambda d: {**d, "vwap": None}, "vwap_alignment"),
        (lambda d: {**d, "volume": {"relative_volume": None}}, "volume_confirmation"),
    ],
)
def test_a_missing_input_scores_zero_for_its_section(removal, section: str) -> None:
    decision = retest(removal(FULL))
    assert decision.score_breakdown[section] == 0


def test_a_missing_benchmark_scores_zero_not_agreement() -> None:
    """ "I could not decide" is not the benchmark agreeing with the trade."""
    decision = retest({**FULL, "relative_strength": {"relative_strength_percent": None}}, {})
    assert decision.score_breakdown["market_confirmation"] == 0


def test_an_instrument_with_no_indicators_at_all_scores_nothing() -> None:
    """The headline case. This used to score 100 of 100.

    EMAs are supplied because their absence is refused before scoring, and the
    point being made here is about the score rather than about the refusal.
    """
    bare = {
        "opening_range": {"high": 110.0, "low": 100.0, "complete": True},
        "ema_fast": 111.0,
        "ema_slow": 106.0,
    }
    decision = retest(bare, {}, {**CONTROLS, "minimum_score": 0})
    assert decision.score_breakdown["breakout_retest"] == 0
    assert decision.score_breakdown["vwap_alignment"] == 0
    assert decision.score_breakdown["volume_confirmation"] == 0
    assert decision.score_breakdown["market_confirmation"] == 0


def test_missing_emas_are_reported_as_missing_not_as_a_choppy_market() -> None:
    """The operator reads this field to find out why nothing traded.

    Telling them the market was ranging when in fact no EMA had been computed
    is the same dishonesty as scoring absent data full marks, moved into prose.
    """
    decision = retest({**FULL, "ema_fast": None, "ema_slow": None})
    assert decision.reason == "EMA values are unavailable"


def test_emas_that_are_present_but_flat_still_report_chop() -> None:
    """Guards the guard: the real chop refusal must survive the split."""
    decision = retest({**FULL, "ema_fast": 110.01, "ema_slow": 110.0})
    assert decision.reason == "EMA spread indicates choppy market"


def test_absence_never_beats_unfavourable_evidence() -> None:
    """The inversion that made this a defect rather than a rounding choice."""
    absent = retest({**FULL, "volume": {"relative_volume": None}})
    unfavourable = retest({**FULL, "volume": {"relative_volume": 0.2}})
    assert absent.score_breakdown["volume_confirmation"] <= unfavourable.score_breakdown["volume_confirmation"]


def test_present_and_strong_still_scores_full_marks() -> None:
    """Guards the guard: scoring everything zero would also pass the tests above."""
    decision = retest(FULL)
    assert decision.score_breakdown["volume_confirmation"] == 20
    assert decision.score_breakdown["ema_alignment"] == 20
    assert decision.next_state == SIGNALLED


# --- a required input blocks rather than discounts ------------------------


def test_a_required_input_that_is_missing_blocks_the_signal() -> None:
    controls = {**CONTROLS, "required_inputs": [RVOL], "minimum_score": 0}
    decision = retest({**FULL, "volume": {"relative_volume": None}}, NIFTY, controls)
    assert decision.next_state == AWAITING
    assert "rvol" in decision.reason
    assert decision.side is None


def test_the_refusal_names_every_missing_input_in_a_stable_order() -> None:
    """An operator comparing two sessions by eye needs the same words twice."""
    controls = {**CONTROLS, "required_inputs": [RVOL, ATR, REGIME], "minimum_score": 0}
    decision = retest({"opening_range": FULL["opening_range"]}, {}, controls)
    assert decision.reason == "Required market data unavailable: atr, regime, rvol"


def test_a_required_input_that_is_present_does_not_block() -> None:
    controls = {**CONTROLS, "required_inputs": [ATR, EMA, RVOL]}
    assert retest(FULL, NIFTY, controls).next_state == SIGNALLED


def test_blocking_beats_scoring_so_a_high_score_cannot_rescue_missing_data() -> None:
    """A strategy without the input it depends on is a different strategy."""
    controls = {**CONTROLS, "required_inputs": [REGIME], "minimum_score": 0}
    decision = retest(FULL, {}, controls)
    assert decision.next_state == AWAITING
    assert "regime" in decision.reason


# --- availability detection ----------------------------------------------


def test_available_inputs_reads_what_the_scorer_reads() -> None:
    assert available_inputs(FULL, NIFTY) == {ATR, VWAP, EMA, RVOL, RELATIVE_STRENGTH, REGIME}


def test_insufficient_data_is_not_a_regime() -> None:
    assert REGIME not in available_inputs(FULL, {"nifty_regime": {"regime": "INSUFFICIENT_DATA"}})


def test_one_half_of_the_ema_pair_is_not_an_ema() -> None:
    assert EMA not in available_inputs({**FULL, "ema_slow": None}, NIFTY)


@pytest.mark.parametrize("shape", [{}, None, {"volume": "nonsense"}, {"relative_strength": []}])
def test_a_shape_we_did_not_expect_reports_absence_rather_than_raising(shape) -> None:
    """The scanner must not crash on a payload it has never seen."""
    assert available_inputs(shape, None) == set()


def test_no_requirement_means_nothing_is_missing() -> None:
    assert missing_required({}, {}, None) == []
    assert missing_required({}, {}, []) == []


# --- configuration --------------------------------------------------------


def test_each_shipped_strategy_declares_what_it_depends_on() -> None:
    for strategy_type, required in DEFAULT_REQUIRED_INPUTS.items():
        assert required, f"{strategy_type} declares no required inputs"
        assert set(required) <= SUPPORTED_INPUTS


def test_a_configuration_written_before_this_field_existed_still_requires_its_inputs() -> None:
    """Empty must mean "use the default", not "require nothing".

    Defaulting to nothing would silently restore the old behaviour for every
    strategy row already in the database, which is the behaviour being fixed.
    """
    strategy = StrategyConfiguration(name="Legacy ORB", strategy_type="orb-retest-v1")
    assert strategy.required_inputs == []
    assert strategy.effective_required_inputs() == DEFAULT_REQUIRED_INPUTS["orb-retest-v1"]
    assert strategy.effective_controls(CONTROLS)["required_inputs"] == DEFAULT_REQUIRED_INPUTS["orb-retest-v1"]


def test_an_operator_choice_overrides_the_default() -> None:
    strategy = StrategyConfiguration(name="Loose ORB", strategy_type="orb-retest-v1", required_inputs=[ATR])
    assert strategy.effective_required_inputs() == [ATR]


def test_an_unknown_required_input_is_refused_at_the_boundary() -> None:
    """Treating an unknown name as unmet would block every signal and look like
    a data outage, which is a long afternoon."""
    with pytest.raises(ValueError, match="Unsupported required input"):
        StrategyConfiguration(name="Broken", required_inputs=["moon_phase"])


def test_duplicates_and_casing_are_normalised() -> None:
    assert normalise_required(["ATR", "atr", " rvol "]) == [ATR, RVOL]


def test_the_effective_controls_always_carry_the_requirement() -> None:
    """The scanner reads required_inputs from here; a missing key fails open."""
    strategy = StrategyConfiguration(name="Any", strategy_type="orb-retest-v1")
    assert "required_inputs" in strategy.effective_controls(CONTROLS)
