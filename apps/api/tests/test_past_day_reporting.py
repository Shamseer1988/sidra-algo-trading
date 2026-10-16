"""Three figures that were true of a different day, or of a day nobody can have.

Each of these reports was honest about what it read and silent about *when* it
read it, which is the same fault three times:

``inspect_live_day --date`` scoped the submissions, the charges and the risk to
the date asked for, then printed the broker's order and position books under the
same heading. Those books take no date -- they are whatever the account holds
when they are called -- so on a past session an operator read today's open
position as that day's.

``check_settings`` section 6 compares what today has used against what today
allows. The used side is totalled from orders already sent, each sized by the
settings in force when it went out; the allowed side is read from the settings
as they stand now. Six account settings were changed across two sessions here,
so "the same configuration" was an assumption, not a fact.

And the profit target had no reachability check at all, though the loss stop has
had one since it was written. A target above the best day the settings permit is
a switch that is permanently off, and a day ending on the trade ceiling with the
target untouched reads as a day the target allowed to run.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.settings_history import changed_on

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_settings  # noqa: E402
import inspect_live_day  # noqa: E402

TODAY = date(2026, 10, 16)


# --- the broker's books are always now -------------------------------------


def test_todays_report_carries_no_disclaimer():
    """The ordinary run. A note on every one of these would be ignored by the
    time it mattered."""
    assert inspect_live_day.stale_book_note(TODAY, today=TODAY) is None


def test_a_past_session_says_the_books_below_are_not_that_days():
    note = inspect_live_day.stale_book_note(date(2026, 10, 15), today=TODAY)
    assert note is not None
    # Both dates, because the whole fault was that only one of them was named.
    assert "2026-10-16" in note
    assert "2026-10-15" in note
    assert "NOT on 2026-10-15" in note
    assert "intraday" in note


def test_a_future_date_is_not_treated_as_stale():
    """A date ahead of today has no broker book to be stale against; the day has
    not happened. Warning about it would be noise with no finding behind it."""
    assert inspect_live_day.stale_book_note(date(2026, 10, 17), today=TODAY) is None


# --- which configuration each half of section 6 describes -------------------


def revision(*, changed: list[str], at: datetime):
    return SimpleNamespace(changed_keys=changed, created_at=at)


def test_a_limit_saved_today_is_named_with_the_time_it_moved():
    """ "maximum_daily_trades at 11:20" tells an operator which of their orders
    were sized before it. "The settings changed" does not."""
    moved = changed_on(
        [revision(changed=["maximum_daily_trades"], at=datetime(2026, 10, 16, 5, 50, tzinfo=UTC))],
        TODAY,
        keys=("maximum_daily_trades", "daily_loss_limit"),
    )
    assert moved == ["maximum_daily_trades at 11:20"]


def test_yesterdays_save_is_not_todays_problem():
    """By the next session the two halves agree again."""
    assert (
        changed_on(
            [revision(changed=["daily_loss_limit"], at=datetime(2026, 10, 15, 5, 50, tzinfo=UTC))],
            TODAY,
            keys=("daily_loss_limit",),
        )
        == []
    )


def test_a_save_that_touched_nothing_this_section_compares_is_silent():
    """An operator who changed the minimum score has not made these rows
    disagree, and saying so would be the row-level updated_at mistake again."""
    assert (
        changed_on(
            [revision(changed=["minimum_score", "volume_multiplier"], at=datetime(2026, 10, 16, 5, 0, tzinfo=UTC))],
            TODAY,
            keys=("daily_loss_limit", "maximum_daily_trades"),
        )
        == []
    )


def test_the_session_date_is_ist_not_utc():
    """23:00 UTC is 04:30 IST the next morning. Filing that save under the UTC
    date would attach it to the session before it."""
    late = revision(changed=["daily_profit_target"], at=datetime(2026, 10, 15, 23, 0, tzinfo=UTC))
    assert changed_on([late], TODAY, keys=("daily_profit_target",)) == ["daily_profit_target at 04:30"]
    assert changed_on([late], date(2026, 10, 15), keys=("daily_profit_target",)) == []


def test_the_latest_move_of_a_key_is_the_one_reported_whatever_the_order():
    """``revision_history`` returns newest first and ``last_changed_per_key``
    walks oldest first. This must not depend on which it is handed."""
    rows = [
        revision(changed=["daily_loss_limit"], at=datetime(2026, 10, 16, 4, 0, tzinfo=UTC)),
        revision(changed=["daily_loss_limit"], at=datetime(2026, 10, 16, 6, 30, tzinfo=UTC)),
    ]
    assert changed_on(rows, TODAY, keys=("daily_loss_limit",)) == ["daily_loss_limit at 12:00"]
    assert changed_on(list(reversed(rows)), TODAY, keys=("daily_loss_limit",)) == ["daily_loss_limit at 12:00"]


def test_every_limit_section_six_compares_is_watched():
    """The list is the point of the check: a limit left out of it changes
    silently, which is the state this replaced."""
    for key in ("maximum_daily_risk_percent", "maximum_open_positions", "daily_loss_limit", "daily_profit_target"):
        assert key in check_settings.PORTFOLIO_LIMITS


# --- a profit target that can never fire ------------------------------------


def strategies(*rewards: float, enabled: bool = True) -> list[dict]:
    return [
        {"id": f"s{index}", "name": f"Strategy {index}", "enabled": enabled, "minimum_rr": reward}
        for index, reward in enumerate(rewards)
    ]


def test_the_reward_risk_comes_from_the_strategies_not_the_account():
    """Every strategy writes its own minimum_rr into the effective controls
    unconditionally, so the account figure governs nothing -- and a target
    judged against it would be judged against a number no trade is sized
    from."""
    reward, source = check_settings.best_reward_multiple(strategies(2.0, 3.0), account_rr=1.5)
    assert reward == Decimal("3.0")
    assert "Strategy 1" in source


def test_a_disabled_strategy_does_not_raise_the_ceiling():
    reward, _ = check_settings.best_reward_multiple([*strategies(2.0), *strategies(9.0, enabled=False)], account_rr=1.5)
    assert reward == Decimal("2.0")


def test_with_no_readable_strategy_the_account_figure_is_used_and_labelled():
    """A fallback that does not say it is one would put a number on the screen
    with the wrong provenance."""
    reward, source = check_settings.best_reward_multiple([{"name": "x"}], account_rr=1.5)
    assert reward == Decimal("1.5")
    assert "account" in source


def test_the_live_settings_cannot_reach_their_own_profit_target():
    """The deployment as saved: 50 a trade, 3 trades, 2:1. The best day is 300
    and the target is 400, so the target can never fire and the day will end on
    the trade ceiling with it untouched."""
    best, basis = check_settings.best_day_available(
        per_trade=Decimal("50"), ceiling=3, funded=3, stored=strategies(2.0), account_rr=2.0
    )
    assert best == Decimal("300.0")
    assert Decimal("400") > best
    assert "3 trade(s) x 50.00 risked x 2.0 reward:risk = 300.00" in basis


def test_the_budget_caps_the_day_when_it_funds_fewer_trades_than_the_ceiling():
    """A ceiling of 5 on a budget that pays for 2 is a day of 2 trades, and the
    best day has to be computed from the number that really binds."""
    best, basis = check_settings.best_day_available(
        per_trade=Decimal("50"), ceiling=5, funded=2, stored=strategies(2.0), account_rr=2.0
    )
    assert best == Decimal("200.0")
    assert "2 trade(s)" in basis


@pytest.mark.parametrize("funded", [0, -1])
def test_an_unfunded_budget_falls_back_to_the_ceiling_rather_than_to_zero(funded: int):
    """A best day of 0 would warn that every target is unreachable, which is a
    false alarm about a settings row this cannot see the cause of."""
    best, _ = check_settings.best_day_available(
        per_trade=Decimal("50"), ceiling=3, funded=funded, stored=strategies(2.0), account_rr=2.0
    )
    assert best == Decimal("300.0")
