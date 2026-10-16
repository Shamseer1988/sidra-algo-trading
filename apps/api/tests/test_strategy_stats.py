"""Judging a strategy needs the comparison nothing on any screen made.

Net P&L is the result, not the verdict. A win rate is neither good nor bad on
its own; it is good or bad against the win rate the reward:risk needs, and 33%
is comfortable at 3:1 and losing at 1.6:1. ``strategy_stats.py`` puts the two
beside each other, and these tests pin the three decisions that make the
comparison mean anything:

**R, not rupees.** Risk per trade moved from 100 to 50 between two consecutive
live sessions here, so a money average over both is weighted towards the
larger-risk half. The test below is built so the money arithmetic and the R
arithmetic reach opposite verdicts on the same two trades -- which is the whole
reason this is not the ``summarise_expectancy`` already on the screen.

**Grouped by strategy, not by saved version.** The version bumps on every
settings save, so grouping by the stored string scatters one strategy across a
row per save.

**The cost of a range is the broker's figure where it has billed and the
estimate where it has not.** Taking the broker's total for the two settled days
of a six-day range and calling it the cost of the range understates it, and the
error always points the same way.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.services.execution_quality import (
    cost_of_trading,
    r_verdict,
    read_verdict,
    strategy_type,
    summarise_by_strategy,
    summarise_expectancy,
    summarise_r,
)
from app.services.trade_history import BROKER, LIVE, DaySummary, TradeRecord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import strategy_stats  # noqa: E402

SESSION = date(2026, 10, 15)


def trade(
    *,
    net="100",
    risk="100",
    charges="12",
    strategy="orb-retest-v1@15",
    day=SESSION,
    open_quantity=0,
):
    """One closed trade. ``net`` and ``risk`` are the only two R depends on."""
    net_d, charges_d = Decimal(net), Decimal(charges)
    return TradeRecord(
        position_id=uuid4(),
        signal_id=uuid4(),
        session_date=day,
        instrument_token="NSE_EQ|INE062A01020",
        script_name="SBIN",
        side="LONG",
        strategy_version=strategy,
        status="OPEN" if open_quantity else "CLOSED",
        execution_mode=LIVE,
        quantity=12,
        open_quantity=open_quantity,
        entry_price=Decimal("820.40"),
        exit_price=None if open_quantity else Decimal("826.10"),
        stop_price=Decimal("813.80"),
        target_price=Decimal("833.60"),
        opened_at=datetime(2026, 10, 15, 4, 32, tzinfo=UTC),
        closed_at=None if open_quantity else datetime(2026, 10, 15, 5, 10, tzinfo=UTC),
        gross_pnl=net_d + charges_d,
        charges=charges_d,
        net_pnl=net_d,
        unrealized_pnl=Decimal("0"),
        risk_amount=Decimal(risk),
        reconciliation="MATCHED",
        reconciliation_note="",
        broker="UPSTOX",
        price_source=BROKER,
        modelled_gross_pnl=net_d + charges_d,
    )


# --- R, and why it is not the money ----------------------------------------


def test_the_break_even_win_rate_is_read_off_r_not_off_rupees():
    """The same two trades, two arithmetics, opposite verdicts.

    A 100 win on 50 risked is 2R; a 140 loss on 200 risked is 0.70R. In R the
    pair needs 25.9% of trades to win and got 50%, so it is ahead. In rupees it
    needs 58.3% and is behind -- because the money average does not know the
    loser was taken at four times the size.

    Position size is an operator's decision made between sessions and R is the
    unit that survives it, which is why this report uses R and the expectancy
    already on the history screen could not be reused.
    """
    records = [trade(net="100", risk="50"), trade(net="-140", risk="200")]

    profile = summarise_r(records)
    assert profile.break_even_win_rate_percent == Decimal("25.9")
    assert profile.win_rate_percent == Decimal("50.0")
    assert "losing edge" not in r_verdict(profile)

    # The money figure, unchanged, for the contrast. Both are honest answers to
    # different questions; only one is still true after the sizing changes.
    assert summarise_expectancy(records).break_even_win_rate_percent == Decimal("58.3")


def test_the_r_averages_split_winners_from_losers():
    records = [trade(net="200", risk="100"), trade(net="100", risk="100"), trade(net="-100", risk="100")]
    profile = summarise_r(records)
    assert profile.average_r_won == Decimal("1.50")
    assert profile.average_r_lost == Decimal("-1.00")
    assert profile.expectancy_r == Decimal("0.67")
    assert profile.reward_risk == Decimal("1.50")


def test_a_scratch_is_neither_a_win_nor_a_loss():
    """An exit at the entry price is not a loss because the net is a few paise
    negative. Counting it as one would understate every win rate here."""
    profile = summarise_r([trade(net="200"), trade(net="-0.40"), trade(net="-200")])
    assert (profile.wins, profile.losses, profile.scratches) == (1, 1, 1)
    assert profile.win_rate_percent == Decimal("50.0")


def test_an_open_position_is_not_a_result():
    """Its figure is still moving. Including it would report a position as a
    finding, which is the one thing a measurement must not be built on."""
    profile = summarise_r([trade(net="100"), trade(net="-9999", open_quantity=12)])
    assert profile.trades == 1
    assert profile.net_pnl == Decimal("100")


def test_a_trade_with_no_planned_risk_has_no_r_and_says_so():
    """Dividing by a risk nobody planned would invent the number the whole
    table turns on. It still counts in the money."""
    profile = summarise_r([trade(net="100", risk="50"), trade(net="-60", risk="0")])
    assert (profile.trades, profile.measured) == (2, 1)
    assert profile.expectancy_r == Decimal("2.00")
    assert profile.net_pnl == Decimal("40")


def test_break_even_is_absent_until_both_sides_have_happened():
    """Not guessed at, and not reported as zero: a break-even of 0% would read
    as a strategy that cannot lose."""
    profile = summarise_r([trade(net="100"), trade(net="150")])
    assert profile.break_even_win_rate_percent is None
    assert profile.reward_risk is None
    assert "no losing trade yet" in r_verdict(profile)


# --- one strategy, however many times it was saved -------------------------


def test_the_saved_version_is_not_part_of_the_strategy():
    assert strategy_type(trade(strategy="orb-retest-v1@15")) == "orb-retest-v1"
    assert strategy_type(trade(strategy="orb-retest-v1@12")) == "orb-retest-v1"
    assert strategy_type(trade(strategy="")) == "unknown"


def test_versions_of_one_strategy_are_one_row():
    """ORB went v12 to v15 in three days without its logic changing once. Four
    rows of two trades each is how a losing strategy hides."""
    records = [
        trade(net="-100", strategy="orb-retest-v1@12"),
        trade(net="-100", strategy="orb-retest-v1@13"),
        trade(net="-100", strategy="orb-retest-v1@15"),
        trade(net="200", strategy="vwap-pullback-v1@4"),
    ]
    rows = summarise_by_strategy(records)
    assert [row.label for row in rows] == ["vwap-pullback-v1", "orb-retest-v1"]
    assert rows[1].trades == 3
    assert rows[1].versions == ("orb-retest-v1@12", "orb-retest-v1@13", "orb-retest-v1@15")


# --- the verdict line ------------------------------------------------------


def test_a_win_rate_below_break_even_is_named_a_losing_edge():
    """The sentence the operator asked for: the two numbers and what they mean
    together, in one line, with no arithmetic left to the reader."""
    records = [trade(net="200", risk="100")] + [trade(net="-100", risk="100") for _ in range(2)]
    verdict = r_verdict(summarise_r(records))
    assert verdict.startswith("Win rate 33.3% vs break-even 33.3%")
    records.append(trade(net="-100", risk="100"))
    verdict = r_verdict(summarise_r(records))
    assert "Win rate 25.0% vs break-even 33.3%: losing edge, 8.3 points short" in verdict
    assert "expectancy -0.25R a trade" in verdict


def test_no_trades_is_said_rather_than_printed_as_zero():
    assert "nothing to judge" in r_verdict(summarise_r([]))


def test_the_sample_caveat_names_the_ledger_it_counted():
    """One sentence, two ledgers. A second copy of it would be a second place
    for the 30-and-100 thresholds to drift."""
    expectancy = summarise_expectancy([trade() for _ in range(4)])
    assert "4 closed live trades" in read_verdict(expectancy)
    assert "4 closed paper trades" in read_verdict(expectancy, ledger="paper")
    assert "not a sample" in read_verdict(expectancy, ledger="paper")


# --- what the range cost ---------------------------------------------------


def day(session_date: date, *, charges: str, broker_charges: str | None, live: int = 1) -> DaySummary:
    return DaySummary(
        session_date=session_date,
        trades=live,
        live_trades=live,
        charges=Decimal(charges),
        broker_charges=None if broker_charges is None else Decimal(broker_charges),
    )


def test_the_cost_of_a_range_is_not_the_broker_total_over_part_of_it():
    """The trap this exists for. Two settled days out of four, and the broker's
    23.35 presented as the cost of the range would be short by the two days it
    has not billed -- a flattering direction, always."""
    days = [
        day(date(2026, 10, 13), charges="12.00", broker_charges="12.20"),
        day(date(2026, 10, 8), charges="11.00", broker_charges="11.15"),
        day(date(2026, 10, 14), charges="23.68", broker_charges=None),
        day(date(2026, 10, 15), charges="18.40", broker_charges=None),
    ]
    cost = cost_of_trading(days)
    assert cost.settled == Decimal("23.35")
    assert cost.settled_days == 2
    assert cost.estimated == Decimal("42.08")
    assert cost.total == Decimal("65.43")
    assert cost.estimated_days == (date(2026, 10, 14), date(2026, 10, 15))
    assert not cost.is_complete


def test_a_fully_settled_range_says_so():
    cost = cost_of_trading([day(date(2026, 10, 13), charges="12.00", broker_charges="12.20")])
    assert cost.is_complete
    assert cost.total == Decimal("12.20")
    assert cost.estimated == Decimal("0")


# --- the script's own reading ----------------------------------------------


def test_the_sizing_note_appears_only_when_the_sizing_changed():
    steady = [trade(risk="100"), trade(risk="100")]
    assert strategy_stats.sizing_note(steady) is None
    mixed = [trade(risk="100"), trade(risk="50")]
    note = strategy_stats.sizing_note(mixed)
    assert note is not None and "50, 100" in note


def test_an_absent_figure_is_a_dash_and_never_a_zero():
    """A 0.00 in the average-win column of a strategy that has not won yet reads
    as a strategy that wins nothing, which is a different claim."""
    assert strategy_stats.number(None, 8).strip() == "-"
    assert strategy_stats.number(Decimal("0"), 8).strip() == "0.00"
