"""Splitting the record at a condition's median -- and refusing to over-read it.

The script's job is to say which entry condition separated the trades that
worked from the ones that did not, on about forty trades. Forty trades is
little enough that every honest answer here is a question, and the two ways
this kind of report misleads are both arithmetic:

  * a median split that puts three trades on one side and thirty-five on the
    other, reported as though the three meant something;
  * a number that is not a condition at all -- the share price, the VWAP level
    -- split at its median, producing a confident row that says expensive
    shares did better, which is a fact about which shares the ranking picked.

Both are tested below, alongside the flattening that decides what counts as a
condition in the first place.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import entry_edge  # noqa: E402


def entry(
    *,
    net_r: float | None = 0.0,
    mfe_r: float | None = None,
    conditions: dict[str, float] | None = None,
    components: dict[str, float] | None = None,
    side: str = "LONG",
    strategy: str = "orb-retest-v1",
) -> entry_edge.Entry:
    return entry_edge.Entry(
        name="TESTCO",
        session_date=date(2026, 11, 6),
        side=side,
        strategy=strategy,
        entered_at=None,
        net_r=net_r,
        won=(net_r or 0.0) > 0,
        score=75,
        mfe_r=mfe_r,
        components=components or {},
        conditions=conditions or {},
    )


# --- what counts as a condition ------------------------------------------


def test_nested_numbers_are_found_by_their_dotted_path():
    """A new indicator starts being measured the day it is added, not the day
    somebody remembers to list it here."""
    flat = entry_edge.flatten({"volume": {"relative_volume": 1.8}, "atr_percent": 0.42})
    assert flat == {"volume.relative_volume": 1.8, "atr_percent": 0.42}


def test_an_absolute_price_is_not_a_condition():
    """Split at its median it says expensive shares did better, which is a fact
    about the ranking's picks and not about an entry."""
    flat = entry_edge.flatten({"last_close": 1240.5, "vwap": 1238.0, "atr_percent": 0.42})
    assert flat == {"atr_percent": 0.42}


def test_a_price_level_nested_under_a_group_is_also_excluded():
    """The denylist is matched on the leaf, so vwap_bands.upper_1 goes too."""
    flat = entry_edge.flatten({"vwap_bands": {"upper_1": 1242.0, "sigma": 3.1}, "extension_atr": 0.8})
    assert flat == {"extension_atr": 0.8}


def test_text_booleans_and_nulls_are_skipped():
    flat = entry_edge.flatten({"regime": "TREND_UP", "is_nifty": True, "gap_percent": None, "atr_percent": 0.5})
    assert flat == {"atr_percent": 0.5}


def test_a_decimal_is_read_as_a_number():
    """Snapshots store money and percents as strings of Decimals in places."""
    assert entry_edge.flatten({"atr_percent": Decimal("0.42")}) == {"atr_percent": 0.42}


# --- the median split -----------------------------------------------------


def test_a_condition_is_split_at_its_own_median():
    entries = [entry(net_r=value, conditions={"rvol": value}) for value in (1.0, 2.0, 3.0, 4.0)]
    found = entry_edge.splits(entries, lambda item: item.conditions, minimum=4)
    assert len(found) == 1
    assert found[0].threshold == 2.5
    assert found[0].below.samples == 2
    assert found[0].above.samples == 2


def test_the_lift_is_the_average_r_above_minus_below():
    entries = [
        entry(net_r=-1.0, conditions={"rvol": 1.0}),
        entry(net_r=-1.0, conditions={"rvol": 1.1}),
        entry(net_r=2.0, conditions={"rvol": 3.0}),
        entry(net_r=2.0, conditions={"rvol": 3.1}),
    ]
    found = entry_edge.splits(entries, lambda item: item.conditions, minimum=4)
    assert found[0].lift == 3.0


def test_a_condition_too_few_trades_carry_is_not_split():
    """Reported as absent rather than as a finding. A split of four is two
    against two, and two trades include both 'excellent' and 'ruinous'."""
    entries = [entry(conditions={"rare": 1.0}) for _ in range(3)]
    assert entry_edge.splits(entries, lambda item: item.conditions, minimum=12) == []


def test_a_constant_is_not_split():
    """Every trade on one side of its own median is a value the strategy already
    gates on. There is nothing to compare, and a row saying so would read as a
    finding that the gate works."""
    entries = [entry(conditions={"minimum_rr": 2.0}) for _ in range(12)]
    assert entry_edge.splits(entries, lambda item: item.conditions, minimum=4) == []


def test_a_trade_missing_a_condition_is_left_out_of_that_split_only():
    """An indicator added mid-record is absent from the earlier trades. Counting
    those as zero would put every one of them below the median."""
    entries = [entry(net_r=1.0, conditions={"new_one": 5.0}) for _ in range(4)]
    entries += [entry(net_r=-1.0, conditions={}) for _ in range(4)]
    entries += [entry(net_r=1.0, conditions={"new_one": 1.0}) for _ in range(4)]
    found = entry_edge.splits(entries, lambda item: item.conditions, minimum=4)
    assert len(found) == 1
    assert found[0].below.samples + found[0].above.samples == 8


def test_a_thin_side_is_flagged():
    """Eight is the line because below it the 95% interval on a win rate is wider
    than the win rate itself. Ten trades split five against five, so the lift is
    a difference between two numbers that each mean almost nothing."""
    entries = [entry(net_r=-1.0, conditions={"rvol": 1.0 + index / 100}) for index in range(5)]
    entries += [entry(net_r=2.0, conditions={"rvol": 9.0 + index / 100}) for index in range(5)]
    found = entry_edge.splits(entries, lambda item: item.conditions, minimum=4)
    assert (found[0].below.samples, found[0].above.samples) == (5, 5)
    assert found[0].thin is True


def test_a_split_with_enough_on_both_sides_is_not_flagged():
    entries = [entry(net_r=-1.0, conditions={"rvol": 1.0 + index / 100}) for index in range(10)]
    entries += [entry(net_r=2.0, conditions={"rvol": 9.0 + index / 100}) for index in range(10)]
    found = entry_edge.splits(entries, lambda item: item.conditions, minimum=4)
    assert (found[0].below.samples, found[0].above.samples) == (10, 10)
    assert found[0].thin is False


def test_a_condition_most_trades_tie_on_is_dropped_rather_than_reported():
    """Ties at the median put every trade on the 'above' side, so there is no
    comparison left. Reported as a finding it would say the gate works."""
    entries = [entry(net_r=-1.0, conditions={"rvol": 1.0}) for _ in range(20)]
    entries += [entry(net_r=2.0, conditions={"rvol": 9.0}) for _ in range(3)]
    assert entry_edge.splits(entries, lambda item: item.conditions, minimum=4) == []


# --- the buckets ----------------------------------------------------------


def test_a_bucket_counts_wins_and_averages_only_measured_r():
    """A trade with no risk_amount has no R. Averaging it as zero would pull
    every average toward break-even, which is the direction that flatters."""
    entries = [entry(net_r=2.0), entry(net_r=-1.0), entry(net_r=None)]
    result = entry_edge.bucket(entries)
    assert result.samples == 3
    assert result.wins == 1
    assert result.average_r == 0.5


def test_an_empty_bucket_does_not_divide_by_zero():
    result = entry_edge.bucket([])
    assert result.samples == 0
    assert result.win_percent == 0.0
    assert result.average_r == 0.0


# --- the excursion bands --------------------------------------------------


def test_the_bands_cover_every_excursion_without_overlapping():
    """Every mfe_r lands in exactly one band, or the shares do not sum to 100%."""
    for value in (0.0, 0.24, 0.25, 0.49, 0.5, 0.99, 1.0, 1.49, 1.5, 1.99, 2.0, 7.5):
        matching = [(low, high) for low, high in entry_edge.BANDS if low <= value < high]
        assert len(matching) == 1, f"{value} matched {matching}"
