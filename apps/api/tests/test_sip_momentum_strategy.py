"""Stocks-in-Play momentum: one decision a day at 10:10 AM, paper only.

Pinned here: the session relative volume it rests on, the single decision
candle, each refusal, the stop half a daily ATR away, and the builder template
that holds to 3 PM instead of taking the account's 1:2 target.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.api.routes.strategy_builder import TEMPLATE_OVERRIDES
from app.services.exit_rules import ExitRules
from app.services.extra_strategies import (
    AWAITING,
    SIGNALLED,
    SIP_CANDIDATE_LONG,
    SIP_CANDIDATE_SHORT,
    SIP_DONE,
    SIP_MOMENTUM_VERSION,
    evaluate_sip_momentum,
    sip_rank,
)
from app.services.market_calculations import CompletedCandle, session_relative_volume
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry

TOKEN = "NSE_EQ|INESIPTEST"
SESSION = date(2026, 11, 30)


def bar(hour: int, minute: int, close: str, volume: int = 100, open_p: str | None = None) -> CompletedCandle:
    """``hour``/``minute`` in UTC: 04:39 UTC is 10:09 AM IST."""
    opened = datetime(2026, 11, 30, hour, minute, tzinfo=UTC)
    open_value = Decimal(open_p or close)
    return CompletedCandle(
        instrument_token=TOKEN,
        timeframe_seconds=60,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=open_value,
        high=max(open_value, Decimal(close)) + Decimal("0.05"),
        low=min(open_value, Decimal(close)) - Decimal("0.05"),
        close=Decimal(close),
        volume=volume,
        tick_count=5,
    )


DECISION = (4, 39)  # 10:09-10:10 AM IST


def configuration(**overrides) -> StrategyConfiguration:  # noqa: ANN003
    values = dict(
        id="sip-momentum-paper",
        name="Stocks-in-Play Momentum (paper)",
        live_orders=False,
        strategy_type=SIP_MOMENTUM_VERSION,
        **TEMPLATE_OVERRIDES[SIP_MOMENTUM_VERSION],
    )
    values.update(overrides)
    return StrategyConfiguration(**values)


BASE_CONTROLS = {
    "account_capital": 50_000.0,
    "risk_per_trade_percent": 0.5,
    "stop_atr_multiple": 2.1,
    "min_stop_distance_percent": 0.8,
    "trade_start_time": "09:45",
    "trade_cutoff_time": "11:30",
}


def controls(**overrides) -> dict:  # noqa: ANN003
    return configuration(**overrides).effective_controls(BASE_CONTROLS)


def indicators(rvol: float | None = 3.0, prior_close: float = 100.0, vwap: float = 101.0) -> dict:
    return {
        "session_rvol": rvol,
        "daily_atr": 2.0,
        "vwap": vwap,
        "atr": 0.1,
        "cpr": {"prior_day": {"high": 101.0, "low": 99.0, "close": prior_close}},
    }


def decide(candle: CompletedCandle, values: dict, state: str = AWAITING, **overrides):  # noqa: ANN003, ANN201
    return evaluate_sip_momentum(candle, values, {}, controls(**overrides), state)


# --- session relative volume ------------------------------------------------


def test_session_relative_volume_compares_the_morning_with_the_same_minutes() -> None:
    active = [bar(3, 45, "100", 300), bar(3, 46, "100", 100)]
    baseline = {"09:15": 100.0, "09:16": 100.0}
    assert session_relative_volume(active, baseline) == 2.0


def test_session_relative_volume_counts_only_minutes_the_baseline_knows() -> None:
    # 09:17 has no history: its 10,000 shares must not inflate the ratio.
    active = [bar(3, 45, "100", 200), bar(3, 47, "100", 10_000)]
    assert session_relative_volume(active, {"09:15": 100.0}) == 2.0


def test_session_relative_volume_without_a_baseline_is_unknown() -> None:
    assert session_relative_volume([bar(3, 45, "100")], {}) is None
    assert session_relative_volume([bar(3, 45, "100")], None) is None


# --- the decision ---------------------------------------------------------


def selected(**extra) -> dict:  # noqa: ANN003
    candidate = {"rank": 6.0, "session_rvol": 3.0, "move_percent": 2.0, "side": "LONG"}
    return {"sip_selected": True, "sip_position": 1, "sip_candidates": 2, "sip_top": 3,
            "sip_candidate": candidate, **extra}


def enter(candle: CompletedCandle, values: dict, state: str = SIP_CANDIDATE_LONG, **selection):  # noqa: ANN003, ANN201
    return evaluate_sip_momentum(candle, values, {}, {**controls(), **selection}, state)


NEXT = (4, 40)  # 10:10-10:11 AM IST, the candle after the decision


def test_a_share_in_play_above_vwap_becomes_a_long_candidate_with_its_rank() -> None:
    decision = decide(bar(*DECISION, "102"), indicators())
    assert decision.next_state == SIP_CANDIDATE_LONG
    assert decision.side is None and decision.entry_price is None
    assert decision.details["rank"] == 6.0 and decision.details["side"] == "LONG"
    assert decision.reason.startswith("Awaiting the 10:10 AM ranking")


def test_a_selected_candidate_is_bought_on_the_next_candle_with_a_half_atr_stop() -> None:
    decision = enter(bar(*NEXT, "102"), indicators(), **selected())
    assert decision.next_state == SIGNALLED and decision.side == "LONG"
    assert decision.entry_price == Decimal("102")
    # Half the daily ATR of 2.0; neither 2.1 x the 1-minute ATR nor 0.3% is wider.
    assert decision.stop_price == Decimal("101")
    # target_rr 10: far enough that the 3 PM square-off is the real exit.
    assert decision.target_price == Decimal("112")
    assert decision.quantity == 250
    assert decision.details["rank"] == 6.0 and decision.details["position"] == 1
    assert "#1 of 2" in decision.reason


def test_a_candidate_outside_the_top_three_is_not_entered() -> None:
    decision = enter(bar(*NEXT, "102"), indicators(), **selected(sip_selected=False, sip_position=4, sip_candidates=5))
    assert decision.next_state == SIP_DONE and decision.side is None
    assert "Not in today's top 3" in decision.reason and "#4 of 5" in decision.reason


def test_without_a_ranking_a_candidate_is_entered_as_a_backtest_needs() -> None:
    decision = enter(bar(*NEXT, "102"), indicators())
    assert decision.next_state == SIGNALLED


def test_a_selected_candidate_with_no_candle_until_after_10_15_is_not_chased() -> None:
    decision = enter(bar(4, 46, "102"), indicators(), **selected())
    assert decision.next_state == SIP_DONE and "Selection window closed" in decision.reason


def test_a_share_in_play_below_vwap_becomes_a_short_candidate() -> None:
    decision = decide(bar(*DECISION, "98"), indicators(vwap=99.0))
    assert decision.next_state == SIP_CANDIDATE_SHORT
    short = enter(bar(*NEXT, "98"), indicators(vwap=99.0), SIP_CANDIDATE_SHORT, **selected())
    assert short.side == "SHORT" and short.stop_price == Decimal("99")


def test_before_10_10_it_waits_and_after_it_never_looks_again() -> None:
    assert decide(bar(4, 38, "102"), indicators()).next_state == AWAITING
    assert decide(bar(4, 50, "102"), indicators(), SIP_DONE).next_state == SIP_DONE
    # A share whose 10:09 candle never arrived does not trade at 10:20.
    late = decide(bar(4, 50, "102"), indicators())
    assert late.next_state == SIP_DONE and late.side is None


def test_only_one_minute_candles_are_evaluated() -> None:
    five = CompletedCandle(**{**bar(4, 35, "102").__dict__, "timeframe_seconds": 300,
                              "closed_at": datetime(2026, 11, 30, 4, 40, tzinfo=UTC)})
    decision = decide(five, indicators())
    assert decision.next_state == AWAITING and "1-minute candles only" in decision.reason


def test_an_ordinary_volume_morning_is_not_in_play() -> None:
    decision = decide(bar(*DECISION, "102"), indicators(rvol=1.8))
    assert decision.next_state == SIP_DONE and decision.side is None
    assert decision.reason.startswith("Not in play")


def test_a_small_move_is_refused() -> None:
    decision = decide(bar(*DECISION, "100.5"), indicators(vwap=100.0))
    assert decision.next_state == SIP_DONE and decision.reason.startswith("Move too small")


def test_the_wrong_side_of_vwap_is_refused() -> None:
    decision = decide(bar(*DECISION, "102"), indicators(vwap=103.0))
    assert decision.next_state == SIP_DONE and "wrong side of VWAP" in decision.reason


def test_missing_history_is_named_not_guessed() -> None:
    decision = decide(bar(*DECISION, "102"), indicators(rvol=None))
    assert decision.next_state == SIP_DONE and "session volume baseline" in decision.reason


def test_thresholds_can_be_set_per_strategy() -> None:
    values = indicators(rvol=2.0)
    assert decide(bar(*DECISION, "102"), values).next_state == SIP_DONE
    assert decide(bar(*DECISION, "102"), values, sip_min_session_rvol=1.5).next_state == SIP_CANDIDATE_LONG


def test_already_signalled_stays_signalled() -> None:
    assert decide(bar(*DECISION, "102"), indicators(), SIGNALLED).next_state == SIGNALLED


def test_rank_is_volume_times_move() -> None:
    assert sip_rank(Decimal("3"), Decimal("-2.5")) == Decimal("7.50")


# --- wiring -----------------------------------------------------------------


def test_registered_with_a_template_that_pins_its_own_stop() -> None:
    assert StrategyRegistry.definition(SIP_MOMENTUM_VERSION).metadata.name == "Stocks-in-Play Momentum"
    template = configuration()
    assert template.exit_rules == ExitRules(
        target_rr=10, square_off_time="15:00", min_stop_distance_percent=0.3, stop_atr_multiple=2.1
    )
    assert template.live_orders is False
    # Pinned, so an account whose own control is the 1.1 default still gets 2.1.
    assert template.effective_controls({**BASE_CONTROLS, "stop_atr_multiple": 1.1})["stop_atr_multiple"] == 2.1
    assert template.minimum_score == 0 and template.max_trades_per_day == 3
    decision = StrategyRegistry.evaluate(
        template, bar(*DECISION, "102"), indicators(), {}, template.effective_controls(BASE_CONTROLS), AWAITING
    )
    assert decision.next_state == SIP_CANDIDATE_LONG


def test_the_stop_is_the_widest_of_the_three() -> None:
    # 2.1 x a 1-minute ATR of 1.0 = 2.1, wider than half the daily ATR (1.0).
    decision = enter(bar(*NEXT, "102"), {**indicators(), "atr": 1.0}, **selected())
    assert decision.stop_price == Decimal("99.9")
