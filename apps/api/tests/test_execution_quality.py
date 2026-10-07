"""Fill rate, slippage and expectancy — the three numbers that decide whether a
strategy is worth running.

Everything they need was already recorded and none of it was reported. The point
of testing this module hard is not the arithmetic; it is the honesty. A
per-trade expectancy computed from nine trades looks exactly like one computed
from nine hundred, and the only thing standing between a reader and that mistake
is the sentence this module writes above the numbers.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services import execution_quality as quality
from app.services.trade_history import BROKER, LIVE, MODEL, DaySummary, TradeRecord

SESSION = date(2026, 10, 7)


def trade(
    *,
    gross="100",
    charges="20",
    net=None,
    risk="100",
    strategy="orb-retest-v1@9",
    modelled=None,
    source=BROKER,
    open_quantity=0,
):
    gross_d, charges_d = Decimal(gross), Decimal(charges)
    return TradeRecord(
        position_id=uuid4(),
        signal_id=uuid4(),
        session_date=SESSION,
        instrument_token="NSE_EQ|INE397D01024",
        script_name="BHARTIARTL",
        side="LONG",
        strategy_version=strategy,
        status="OPEN" if open_quantity else "CLOSED",
        execution_mode=LIVE,
        quantity=5,
        open_quantity=open_quantity,
        entry_price=Decimal("1818.10"),
        exit_price=None if open_quantity else Decimal("1838.10"),
        stop_price=Decimal("1803.50"),
        target_price=Decimal("1847.50"),
        opened_at=datetime(2026, 10, 7, 5, 36, tzinfo=UTC),
        closed_at=None if open_quantity else datetime(2026, 10, 7, 8, 0, tzinfo=UTC),
        gross_pnl=gross_d,
        charges=charges_d,
        net_pnl=Decimal(net) if net is not None else gross_d - charges_d,
        unrealized_pnl=Decimal("0"),
        risk_amount=Decimal(risk),
        reconciliation="MATCHED",
        reconciliation_note="",
        broker="UPSTOX",
        price_source=source,
        modelled_gross_pnl=Decimal(modelled if modelled is not None else gross),
    )


# --- the sample, said out loud before any number is read ---------------------


def test_no_trades_says_there_is_nothing_to_measure():
    assert "nothing here to measure" in quality.read_verdict(quality.summarise_expectancy([]))


def test_a_handful_of_trades_is_named_as_not_a_sample():
    """The sentence that stops nine trades being read as a finding."""
    verdict = quality.read_verdict(quality.summarise_expectancy([trade() for _ in range(9)]))
    assert "not a sample" in verdict
    assert "evidence about execution" in verdict


def test_a_middling_sample_is_provisional_not_actionable():
    verdict = quality.read_verdict(quality.summarise_expectancy([trade() for _ in range(40)]))
    assert "provisional" in verdict and "not enough to act on it" in verdict


def test_a_real_sample_says_so():
    verdict = quality.read_verdict(quality.summarise_expectancy([trade() for _ in range(120)]))
    assert "worth acting on" in verdict


# --- expectancy --------------------------------------------------------------


def test_an_open_trade_is_not_a_result():
    """A position that is still moving is the one thing a measurement must not
    be built on."""
    result = quality.summarise_expectancy([trade(open_quantity=5), trade()])
    assert result.trades == 1


def test_wins_losses_and_the_rate_between_them():
    records = [trade(gross="200", charges="20") for _ in range(3)] + [trade(gross="-80", charges="20")]
    result = quality.summarise_expectancy(records)
    assert (result.wins, result.losses, result.trades) == (3, 1, 4)
    assert result.win_rate_percent == Decimal("75.0")
    assert result.average_win == Decimal("180.00")
    assert result.average_loss == Decimal("-100.00")
    assert result.net_per_trade == Decimal("110.00")


def test_the_break_even_win_rate_is_what_the_profile_needs_to_come_out_level():
    """The single most useful number on the screen: against the real win rate it
    says whether the edge covers its own costs."""
    records = [trade(gross="150", charges="24")] + [trade(gross="-100", charges="24")]
    result = quality.summarise_expectancy(records)
    # Winner +126, loser -124: 124 / 250.
    assert result.average_win == Decimal("126.00")
    assert result.average_loss == Decimal("-124.00")
    assert result.break_even_win_rate_percent == Decimal("49.6")


def test_a_side_that_has_never_happened_leaves_the_break_even_absent():
    result = quality.summarise_expectancy([trade(gross="150", charges="24")])
    assert result.break_even_win_rate_percent is None


def test_a_scratch_is_neither_a_win_nor_a_loss():
    result = quality.summarise_expectancy([trade(gross="20", charges="20")])
    assert (result.wins, result.losses, result.scratches) == (0, 0, 1)


# --- slippage ----------------------------------------------------------------


def test_trade_slippage_counts_only_broker_priced_rows():
    """A modelled row has no broker fill behind it, so its "slippage" would be
    zero by construction and would drag every average toward nothing."""
    records = [
        trade(gross="90", modelled="100", source=BROKER),
        trade(gross="100", modelled="100", source=MODEL),
    ]
    summary = quality.summarise_slippage([], records)
    assert summary.trade_trades == 1
    assert summary.trade_total == Decimal("-10.00")


def test_entry_slippage_averages_what_the_fills_cost_against_the_plan():
    summary = quality.summarise_slippage([Decimal("0.09"), Decimal("0.10"), Decimal("-0.04")], [])
    assert summary.entry_trades == 3
    assert summary.entry_average == Decimal("0.05")
    assert summary.entry_worst == Decimal("0.10")


def test_nothing_measured_leaves_the_averages_absent():
    summary = quality.summarise_slippage([], [])
    assert summary.entry_average is None and summary.trade_average is None


# --- the funnel --------------------------------------------------------------


def test_the_fill_rate_leaves_out_what_it_does_not_know():
    """Not knowing and knowing it missed are different facts, and averaging them
    would invent a fill rate."""
    funnel = quality.Funnel(sent=10, filled=6, unknown=2)
    assert funnel.fill_rate_percent == Decimal("75.0")


def test_a_fill_rate_with_nothing_decided_is_absent_not_zero():
    assert quality.Funnel(sent=3, filled=0, unknown=3).fill_rate_percent is None
    assert quality.Funnel().fill_rate_percent is None


# --- charges -----------------------------------------------------------------


def day(*, broker_charges=None, live_trades=1):
    return DaySummary(
        session_date=SESSION,
        live_trades=live_trades,
        broker_charges=None if broker_charges is None else Decimal(broker_charges),
    )


def test_the_brokers_own_charges_are_preferred_over_the_estimate():
    charges = quality.summarise_charges([trade(gross="200", charges="12")], [day(broker_charges="37.65")])
    assert charges.estimated == Decimal("12")
    assert charges.broker == Decimal("37.65")
    # The drag is measured from what was really charged, not what was guessed.
    assert charges.per_trade == Decimal("37.65")
    assert charges.percent_of_gross == Decimal("18.8")


def test_an_unsettled_day_falls_back_to_the_estimate_and_says_so():
    charges = quality.summarise_charges([trade(gross="200", charges="12")], [day(broker_charges=None)])
    assert charges.broker is None
    assert charges.days_pending == 1
    assert charges.per_trade == Decimal("12.00")


def test_a_gross_loss_leaves_the_percentage_absent():
    """A ratio against a gross loss is a number that looks like a percentage and
    means nothing."""
    charges = quality.summarise_charges([trade(gross="-50", charges="20")], [])
    assert charges.percent_of_gross is None


# --- the notes ---------------------------------------------------------------


def test_a_low_fill_rate_is_called_out():
    notes = quality.build_notes(
        quality.Funnel(sent=10, filled=4), quality.Slippage(), quality.Charges(), quality.Expectancy()
    )
    assert any("filled" in note and "entry cap" in note for note in notes)


def test_charges_eating_the_gross_are_called_out():
    charges = quality.Charges(gross=Decimal("100"), broker=Decimal("80"), percent_of_gross=Decimal("80.0"))
    notes = quality.build_notes(quality.Funnel(), quality.Slippage(), charges, quality.Expectancy())
    assert any("deciding the result" in note for note in notes)


def test_a_quiet_report_says_nothing():
    assert quality.build_notes(quality.Funnel(), quality.Slippage(), quality.Charges(), quality.Expectancy()) == []


def test_a_win_rate_below_break_even_is_only_called_out_on_a_real_sample():
    """Below thirty trades the gap is noise, and naming it would be the exact
    mistake the verdict above exists to prevent."""
    expectancy = quality.Expectancy(
        trades=9, win_rate_percent=Decimal("30.0"), break_even_win_rate_percent=Decimal("50.0")
    )
    assert quality.build_notes(quality.Funnel(), quality.Slippage(), quality.Charges(), expectancy) == []

    expectancy.trades = 60
    notes = quality.build_notes(quality.Funnel(), quality.Slippage(), quality.Charges(), expectancy)
    assert any("below what this profile needs" in note for note in notes)


# --- per strategy ------------------------------------------------------------


def test_strategies_are_ranked_by_what_they_made():
    rows = quality.summarise_strategies(
        [
            trade(strategy="orb-retest-v1@9", gross="200", charges="20"),
            trade(strategy="rs-pullback-v1@4", gross="-50", charges="20"),
            trade(strategy="rs-pullback-v1@4", gross="-30", charges="20"),
        ]
    )
    assert [row.strategy_version for row in rows] == ["orb-retest-v1@9", "rs-pullback-v1@4"]
    assert rows[0].net == Decimal("180")
    assert rows[1].trades == 2


def test_an_open_trade_belongs_to_no_strategy_yet():
    assert quality.summarise_strategies([trade(open_quantity=5)]) == []


@pytest.mark.parametrize("count", [0, 1, 29, 30, 99, 100])
def test_every_sample_size_gets_a_verdict_naming_its_own_size(count):
    """No number on this screen is ever shown without the count behind it."""
    verdict = quality.read_verdict(quality.summarise_expectancy([trade() for _ in range(count)]))
    assert verdict
    assert str(count) in verdict if count else "nothing here to measure" in verdict
