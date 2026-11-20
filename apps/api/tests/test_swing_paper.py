"""Swing desk Phase 3: the paper desk, run evening by evening against the database.

The claim that matters most: on the same candles, the paper desk makes exactly
the trades the backtest makes -- same days, same prices, same reasons, same net
rupees. A paper desk that disagreed with its own backtest would make the
backtest worthless as evidence for it.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models import ApplicationSetting, MarketCandle, SwingPaperPosition
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.swing_backtest import CostModel, SwingRules, run_backtest
from app.services.swing_data import MARKET_TIMEZONE
from app.services.swing_paper import (
    CANCELLED,
    CLOSED,
    OPEN,
    PLANNED,
    PROGRESS_KEY,
    days_to_process,
    results_due,
    run_evening,
    summary_message,
)
from app.services.swing_strategies import SwingSetup
from app.swing_worker import last_final_day, market_quiet

START = date(2031, 6, 2)


def day(n: int) -> date:
    return START + timedelta(days=n)


def candle(token: str, n: int, o, h, l, c) -> CompletedCandle:  # noqa: E741
    d = day(n)
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=86_400,
        opened_at=datetime.combine(d, time(9, 15), tzinfo=MARKET_TIMEZONE),
        closed_at=datetime.combine(d, time(15, 30), tzinfo=MARKET_TIMEZONE),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=1000,
        tick_count=1,
    )


def series_for(token: str, days: int, overrides: dict[int, tuple]) -> list[CompletedCandle]:
    bars = [candle(token, n, 100, 100.5, 99.5, 100) for n in range(days)]
    for n, ohlc in overrides.items():
        bars[n] = candle(token, n, *ohlc)
    return bars


def stub(fires: dict[str, list[int]]):
    """Fires for a token on the given evenings: buy-stop 101, stop 96, target 111."""

    def fn(series, _bench, i):
        token = series.bars[i].instrument_token
        if series.bars[i].session_date not in {day(n) for n in fires.get(token, [])}:
            return None
        return SwingSetup(
            "stub", series.bars[i].session_date, Decimal("101"), Decimal("96"), Decimal("111"), 1.0, 12, "low10"
        )

    return fn


RULES = SwingRules(regime_sma=2, strategies=("stub",))
INDEX = [candle("NIFTY", n, 100 + n, 101 + n, 99 + n, 100 + n) for n in range(60)]


@pytest.fixture(autouse=True)
async def clean():
    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(SwingPaperPosition))
            await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == PROGRESS_KEY))
            await session.commit()

    await wipe()
    yield
    await wipe()


async def paper_run(universe, strategy, *, days: int, events=(), costs=None, start: int = 0, new_entries: bool = True):
    """The desk, one evening at a time, each seeing only candles up to that evening."""
    for n in range(start, days):
        seen = {token: [bar for bar in bars if bar.session_date <= day(n)] for token, bars in universe.items()}
        index = [bar for bar in INDEX if bar.session_date <= day(n)]
        async with SessionLocal() as session:
            await run_evening(
                session,
                day(n),
                universe=seen,
                benchmark=index,
                sectors={token: token for token in universe},
                symbols={token: token for token in universe},
                events=events,
                rules=RULES,
                costs=costs or CostModel(),
                strategies={"stub": strategy},
                new_entries=new_entries,
            )
            await session.commit()
    async with SessionLocal() as session:
        return list((await session.scalars(select(SwingPaperPosition))).all())


# --- the paper desk agrees with the backtest --------------------------------------


async def test_the_paper_desk_makes_exactly_the_backtests_trades():
    universe = {
        # Fills day 6, half at 2R on day 8, then the 10-session low trail.
        "A": series_for(
            "A",
            40,
            {
                6: (100, 102, 99.5, 101.5),
                8: (102, 111.5, 101.5, 110),
                **{n: (110, 110.5, 109.5, 110) for n in range(9, 20)},
                # The target is touched again: a desk that forgot the half is
                # already sold would sell another half here.
                10: (110, 112, 109.5, 110),
                20: (109, 109.5, 104, 105),
            },
        ),
        # Fills day 6, gaps through the stop on day 9.
        "B": series_for("B", 40, {6: (100, 102, 99.5, 101.5), 9: (90, 91, 89, 90)}),
        # Plan on day 5 lapses; a second plan on day 12 gaps over the trigger.
        "C": series_for("C", 40, {13: (104, 105, 103, 104)}),
    }
    strategy = stub({"A": [5], "B": [5], "C": [5, 12]})

    backtest = run_backtest(
        universe, {t: t for t in universe}, INDEX[:40], rules=RULES, costs=CostModel(), strategies={"stub": strategy}
    )
    rows = await paper_run(universe, strategy, days=40)

    closed = sorted((row for row in rows if row.status == CLOSED), key=lambda row: row.instrument_token)
    expected = sorted((t for t in backtest.trades if t.exit_reason != "open at end"), key=lambda t: t.token)
    # A took half at 2R and reached its 12-session limit; B was stopped through a gap.
    assert [t.exit_reason for t in expected] == ["time", "stop"]
    assert expected[0].partial_done
    assert [
        (r.instrument_token, r.entry_date, r.entry_price, r.exit_date, r.exit_reason, r.net_pnl) for r in closed
    ] == [
        (t.token, t.entry_date, t.entry_price, t.exit_date, t.exit_reason, t.net_pnl.quantize(Decimal("0.01")))
        for t in expected
    ]
    lapsed = sorted(row.exit_reason for row in rows if row.status == CANCELLED)
    assert lapsed == sorted(["not triggered", "gapped above trigger"])
    assert backtest.skipped["not triggered"] == 1 and backtest.skipped["gapped above trigger"] == 1


async def test_the_partial_exit_records_the_price_it_actually_sold_at():
    universe = {"A": series_for("A", 30, {6: (100, 102, 99.5, 101.5), 8: (113, 114, 112, 113), 9: (90, 91, 89, 90)})}
    [row] = await paper_run(universe, stub({"A": [5]}), days=30)
    assert row.partial_exit_price == Decimal("113")  # gapped above the 111 target: sold at the open


# --- each evening once -----------------------------------------------------------------


async def test_running_the_same_evening_twice_changes_nothing():
    universe = {"A": series_for("A", 20, {6: (100, 102, 99.5, 101.5)})}
    strategy = stub({"A": [5]})
    await paper_run(universe, strategy, days=8)
    async with SessionLocal() as session:
        before = [
            (r.status, (r.snapshot or {}).get("state"))
            for r in (await session.scalars(select(SwingPaperPosition))).all()
        ]
    # The worker restarts and runs evening 7 again.
    async with SessionLocal() as session:
        seen = {"A": [b for b in universe["A"] if b.session_date <= day(7)]}
        await run_evening(
            session,
            day(7),
            universe=seen,
            benchmark=INDEX[:8],
            sectors={"A": "A"},
            symbols={"A": "A"},
            rules=RULES,
            strategies={"stub": strategy},
        )
        await session.commit()
        after = [
            (r.status, (r.snapshot or {}).get("state"))
            for r in (await session.scalars(select(SwingPaperPosition))).all()
        ]
    assert after == before


def test_a_first_run_starts_from_the_latest_day_not_the_whole_history():
    available = [day(n) for n in range(10)]
    assert days_to_process(available, None) == [day(9)]
    assert days_to_process(available, day(6)) == [day(7), day(8), day(9)]
    assert days_to_process(available, day(9)) == []


# --- results dates ---------------------------------------------------------------------


def results(token: str, n: int):
    return SimpleNamespace(event_type="RESULTS", ex_date=day(n), instrument_token=token)


def test_results_due_counts_calendar_days_ahead():
    assert results_due([results("A", 8)], day(5), 3) == {"A": f"results on {day(8).isoformat()}"}
    assert results_due([results("A", 9)], day(5), 3) == {}


async def test_no_plan_is_made_with_results_close_ahead():
    universe = {"A": series_for("A", 12, {})}
    rows = await paper_run(universe, stub({"A": [5]}), days=6, events=[results("A", 8)])
    assert rows == []


async def test_an_open_position_is_sold_at_the_next_open_before_results():
    universe = {"A": series_for("A", 20, {6: (100, 102, 99.5, 101.5)})}
    rows = await paper_run(universe, stub({"A": [5]}), days=12, events=[results("A", 11)])
    [row] = rows
    assert row.status == CLOSED and row.exit_reason == "results"
    assert row.exit_date == day(9)  # marked on evening 8 (results within 3 days), sold at the open of day 9


# --- the message, the worker's clock, and the boundary -------------------------------------


async def test_the_evening_message_says_paper_only_and_lists_the_plan():
    rows = await paper_run({"A": series_for("A", 8, {})}, stub({"A": [5]}), days=6)
    [row] = rows
    assert row.status == PLANNED
    from app.services.swing_paper import EveningReport

    text = summary_message(EveningReport(day=day(5), regime_ok=True, planned=[row]))
    assert "PAPER ONLY" in text and "No real order" in text
    assert "buy-stop 101" in text


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        (datetime(2026, 11, 23, 10, 0, tzinfo=MARKET_TIMEZONE), True),
        (datetime(2026, 11, 23, 15, 45, tzinfo=MARKET_TIMEZONE), False),
        (datetime(2026, 11, 22, 11, 0, tzinfo=MARKET_TIMEZONE), False),
    ],
)
def test_the_worker_keeps_out_of_market_hours(moment, quiet):
    assert market_quiet(moment) is quiet


def test_todays_candle_is_trusted_only_after_six_in_the_evening():
    assert last_final_day(datetime(2026, 11, 23, 16, 0, tzinfo=MARKET_TIMEZONE)) == date(2026, 11, 22)
    assert last_final_day(datetime(2026, 11, 23, 18, 30, tzinfo=MARKET_TIMEZONE)) == date(2026, 11, 23)


@pytest.mark.parametrize("module", ["app/services/swing_paper.py", "app/swing_worker.py", "app/api/routes/swing.py"])
def test_the_paper_desk_has_no_path_to_an_order(module):
    source = (Path(__file__).resolve().parents[1] / module).read_text(encoding="utf-8")
    for forbidden in (
        "live_orders",
        "live_execution",
        "live_entry_bridge",
        "submit_live_order",
        "LiveOrderRequest",
        "upstox_orders",
    ):
        assert forbidden not in source, f"{module} mentions {forbidden}"


def test_status_values_match_the_database_constraint():
    assert {PLANNED, OPEN, CLOSED, CANCELLED} == {"PLANNED", "OPEN", "CLOSED", "CANCELLED"}


async def test_the_summary_shows_open_and_planned_positions_with_their_numbers():
    from app.api.routes.swing import summary

    universe = {"A": series_for("A", 12, {6: (100, 102, 99.5, 101.5)}), "B": series_for("B", 12, {})}
    await paper_run(universe, stub({"A": [5], "B": [9]}), days=10)
    async with SessionLocal() as session:
        for bar in universe["A"][:10]:
            session.add(
                MarketCandle(
                    instrument_token="A",
                    timeframe_seconds=86_400,
                    session_date=bar.session_date,
                    opened_at=bar.opened_at,
                    closed_at=bar.closed_at,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    tick_count=1,
                )
            )
        await session.commit()
        try:
            result = await summary(None, session)
        finally:
            await session.execute(delete(MarketCandle).where(MarketCandle.instrument_token == "A"))
            await session.commit()
    assert result.paper_only is True
    assert result.strategies == ["swing-trend-pullback-v1"]
    assert "did not pass" in result.status_note.lower()
    [held] = result.open_positions
    assert held.symbol == "A" and held.entry_price == 101.0 and held.last_close == 100.0
    assert held.unrealised == pytest.approx(-1.0 * held.quantity)
    assert result.open_risk == pytest.approx((101 - 96) * held.quantity)
    assert [plan.symbol for plan in result.planned] == ["B"]
    assert result.planned[0].next_action == "Buy-stop at 101.0000 next session"


async def test_switched_off_the_desk_still_closes_what_it_holds_and_buys_nothing_new():
    """A switch never abandons a position: off cancels waiting plans, and manages the rest to its exit."""
    universe = {
        "A": series_for("A", 30, {6: (100, 102, 99.5, 101.5), 12: (90, 91, 89, 90)}),
        "B": series_for("B", 30, {}),
    }
    strategy = stub({"A": [5], "B": [8, 15]})
    await paper_run(universe, strategy, days=9)  # A bought on day 6; B planned on evening 8
    rows = await paper_run(universe, strategy, start=9, days=20, new_entries=False)
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["A"].status == CLOSED and by_symbol["A"].exit_reason == "stop"
    assert by_symbol["A"].exit_date == day(12)
    assert by_symbol["B"].status == CANCELLED and by_symbol["B"].exit_reason == "desk switched off"
    assert len(rows) == 2  # B's setup on evening 15 made no new plan


def test_the_paper_desk_runs_trend_pullback_only_and_the_worker_says_so():
    """Base Breakout lost money in-sample and out; it does not run on paper."""
    from app.services.swing_paper import PAPER_STRATEGIES
    from app.services.swing_strategies import TREND_PULLBACK

    assert PAPER_STRATEGIES == (TREND_PULLBACK,)
    source = (Path(__file__).resolve().parents[1] / "app" / "swing_worker.py").read_text(encoding="utf-8")
    assert "rules=SwingRules(strategies=PAPER_STRATEGIES)" in source
