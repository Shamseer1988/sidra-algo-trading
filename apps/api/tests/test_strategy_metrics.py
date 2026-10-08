"""What the strategy cards count, and over what.

The endpoint read the most recent 1000 evaluation rows and grouped them by
strategy. The window was therefore 1000 rows **in total**, shared by every
writer: with four strategies and the data-quality gate, each card saw about
200 rows, which is a few minutes of scanning rather than a day. A screenshot
on 10 October showed exactly that -- 200 + 200 + 198 + 195 + 207 = 1000.

Two things followed. Opened outside market hours the newest rows are the last
ones written, after the entry cutoff, where the trade window refuses
everything -- so every card read 0% with certainty rather than as a
measurement. And the quality gate appeared as a fifth strategy that could
never accept anything, because the one place it is constructed hardcodes
``status="REJECTED"``.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.api.routes.settings import METRIC_SESSIONS, QUALITY_GATE_ID, strategy_metrics
from app.db.models import ScannerEvaluation
from app.db.session import SessionLocal

pytestmark = pytest.mark.anyio


def evaluation(
    key: str,
    *,
    session_date: date,
    status: str,
    strategy_id: str = "orb-retest-default",
    strategy_name: str = "ORB Retest — Default",
) -> ScannerEvaluation:
    return ScannerEvaluation(
        evaluation_key=key,
        instrument_token="NSE_EQ|METRICS",
        session_date=session_date,
        candle_opened_at=datetime(2099, 5, 4, 4, 0, tzinfo=UTC),
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        strategy_version=1,
        status=status,
        decision_state="SIGNALLED" if status == "ACCEPTED" else "AWAITING_BREAKOUT",
        reason="test",
        failed_conditions=[],
        data_quality_state="GOOD",
        candle_close=Decimal("100"),
        candle_volume=100,
        score=90,
    )


async def seed(rows: list[ScannerEvaluation]) -> None:
    """Replace this module's own rows, and never anyone else's.

    An earlier draft deleted every ScannerEvaluation to test the empty case,
    which under random ordering emptied the table for the other modules that
    seed it. The sessions here are in 2099 so that these rows are always the
    most recent in the table: the endpoint's window is the last few distinct
    session dates, and a fixture dated in the past could be pushed out of it
    by whatever else happened to be stored.
    """
    async with SessionLocal() as session:
        await session.execute(
            ScannerEvaluation.__table__.delete().where(ScannerEvaluation.instrument_token == "NSE_EQ|METRICS")
        )
        for row in rows:
            session.add(row)
        await session.commit()


async def metrics():
    async with SessionLocal() as session:
        return await strategy_metrics(None, session)


async def test_the_quality_gate_is_not_reported_as_a_strategy() -> None:
    """It writes an evaluation row so a candle nobody scored still leaves a
    record of why. Its status is hardcoded REJECTED, so shown beside the
    strategies it was a fifth one permanently at 0%."""
    day = date(2099, 5, 4)
    await seed(
        [
            evaluation("m-orb-1", session_date=day, status="REJECTED"),
            evaluation(
                "m-dq-1",
                session_date=day,
                status="REJECTED",
                strategy_id=QUALITY_GATE_ID,
                strategy_name="Data quality gate",
            ),
        ]
    )
    reported = {item.strategy_id for item in await metrics()}
    assert QUALITY_GATE_ID not in reported
    assert "orb-retest-default" in reported


async def test_the_quality_gate_does_not_eat_the_strategies_row_budget() -> None:
    """It consumed 207 of the 1000 rows the real strategies needed."""
    day = date(2099, 5, 4)
    rows = [evaluation(f"m-orb-{index}", session_date=day, status="REJECTED") for index in range(5)]
    rows += [
        evaluation(
            f"m-dq-{index}",
            session_date=day,
            status="REJECTED",
            strategy_id=QUALITY_GATE_ID,
            strategy_name="Data quality gate",
        )
        for index in range(50)
    ]
    await seed(rows)
    orb = next(item for item in await metrics() if item.strategy_id == "orb-retest-default")
    assert orb.evaluations == 5


async def test_an_accepted_signal_is_counted_however_old_the_session_is() -> None:
    """The point of the fix: an acceptance earlier in the window is not pushed
    out by a later flood of rejections, which is what a row cap did."""
    day = date(2099, 5, 4)
    rows = [evaluation("m-accept", session_date=day, status="ACCEPTED")]
    rows += [evaluation(f"m-reject-{index}", session_date=day, status="REJECTED") for index in range(99)]
    await seed(rows)
    orb = next(item for item in await metrics() if item.strategy_id == "orb-retest-default")
    assert orb.accepted == 1
    assert orb.rejected == 99
    assert orb.evaluations == 100
    assert orb.acceptance_rate == 1.0


async def test_each_card_says_which_sessions_it_covers() -> None:
    """A rate with no window cannot be read: the same 0% meant a healthy
    scanner at three in the morning and a broken one at eleven."""
    await seed(
        [
            evaluation("m-day1", session_date=date(2099, 5, 4), status="REJECTED"),
            evaluation("m-day2", session_date=date(2099, 5, 5), status="ACCEPTED"),
        ]
    )
    orb = next(item for item in await metrics() if item.strategy_id == "orb-retest-default")
    assert orb.sessions == 2
    assert orb.first_session == date(2099, 5, 4)
    assert orb.last_session == date(2099, 5, 5)


async def test_the_window_is_trading_sessions_and_not_calendar_days() -> None:
    """Keyed by session_date, so a Monday shows the Friday beside it rather
    than an empty weekend. 2 and 5 May 2099 are a Friday and a Monday."""
    days = [date(2099, 5, 1), date(2099, 5, 2), date(2099, 5, 5), date(2099, 5, 6), date(2099, 5, 7)]
    await seed([evaluation(f"m-{day}", session_date=day, status="REJECTED") for day in days])
    orb = next(item for item in await metrics() if item.strategy_id == "orb-retest-default")
    assert orb.sessions == min(len(days), METRIC_SESSIONS)
    assert orb.evaluations == min(len(days), METRIC_SESSIONS)


async def test_a_card_counts_only_the_sessions_it_has_rows_for() -> None:
    """Counting distinct dates across every strategy would let a card claim a
    session it never evaluated in: a strategy added on Thursday would say
    "5 sessions" beside one that had been running all week."""
    await seed(
        [
            evaluation("m-old-1", session_date=date(2099, 5, 1), status="REJECTED", strategy_id="veteran"),
            evaluation("m-old-2", session_date=date(2099, 5, 2), status="REJECTED", strategy_id="veteran"),
            evaluation("m-old-3", session_date=date(2099, 5, 5), status="REJECTED", strategy_id="veteran"),
            evaluation("m-new-1", session_date=date(2099, 5, 5), status="REJECTED", strategy_id="newcomer"),
        ]
    )
    reported = {item.strategy_id: item for item in await metrics()}
    assert reported["veteran"].sessions == 3
    assert reported["newcomer"].sessions == 1
    assert reported["newcomer"].first_session == date(2099, 5, 5)


async def test_sessions_beyond_the_window_are_left_out() -> None:
    days = [date(2099, 5, 1), date(2099, 5, 2), date(2099, 5, 5), date(2099, 5, 6), date(2099, 5, 7), date(2099, 5, 8)]
    await seed([evaluation(f"m-{day}", session_date=day, status="REJECTED") for day in days])
    orb = next(item for item in await metrics() if item.strategy_id == "orb-retest-default")
    assert orb.sessions == METRIC_SESSIONS
    assert orb.first_session == date(2099, 5, 2), "the oldest session must fall out of the window"
