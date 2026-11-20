"""U4: the swing desk's data, backtest and corporate-event tools, run from the screens.

The claims that matter: heavy jobs are refused during market hours; only one
runs at a time, and one abandoned by a restart does not block forever; a
backtest from the screen uses the Settings > Charges card and keeps its result;
a recorded split or demerger adjusts prices and a bad one is refused.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import delete

from app.api.routes import swing_tools as routes
from app.core.config import get_settings
from app.db.models import ApplicationSetting, CorporateEvent
from app.db.session import SessionLocal
from app.services import swing_tools
from app.services.market_calculations import MARKET_TIMEZONE
from app.services.swing_data import adjust_for_events, confirmed_adjustments
from app.services.swing_universe import nifty200

ADMIN = SimpleNamespace(id=None)
MEMBER = nifty200()[0]
# The fixture below replaces swing_tools.market_hours; this is the real one.
REAL_MARKET_HOURS = swing_tools.market_hours


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.setattr(swing_tools, "market_hours", lambda now=None: False)
    keys = (swing_tools.JOBS_KEY, swing_tools.RUNS_KEY)
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key.in_(keys)))
        await session.execute(delete(CorporateEvent).where(CorporateEvent.source == "screen"))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key.in_(keys)))
        await session.execute(delete(CorporateEvent).where(CorporateEvent.source == "screen"))
        await session.commit()


# --- market hours and one job at a time ------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "closed"),
    [
        (datetime(2026, 11, 23, 10, 0, tzinfo=MARKET_TIMEZONE), False),  # Monday, session
        (datetime(2026, 11, 23, 15, 35, tzinfo=MARKET_TIMEZONE), False),
        (datetime(2026, 11, 23, 15, 36, tzinfo=MARKET_TIMEZONE), True),
        (datetime(2026, 11, 23, 8, 59, tzinfo=MARKET_TIMEZONE), True),
        (datetime(2026, 11, 22, 11, 0, tzinfo=MARKET_TIMEZONE), True),  # Sunday
    ],
)
def test_market_hours_are_weekdays_from_nine_to_three_thirty_five(moment, closed):
    assert REAL_MARKET_HOURS(moment) is not closed


async def test_a_heavy_job_is_refused_during_market_hours(monkeypatch):
    monkeypatch.setattr(swing_tools, "market_hours", lambda now=None: True)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.start_backfill(routes.BackfillRequest(), BackgroundTasks(), get_settings(), session, ADMIN)
    assert refused.value.status_code == 409
    assert "3:35 PM" in refused.value.detail
    assert (await swing_tools.jobs()) == {}


async def test_only_one_job_runs_at_a_time():
    await swing_tools.claim("backfill", None, {})
    with pytest.raises(swing_tools.SwingToolError, match="already running"):
        await swing_tools.claim("backtest", None, {})
    await swing_tools.finish("backfill", "done", "ok")
    await swing_tools.claim("backtest", None, {})


async def test_a_job_abandoned_by_a_restart_stops_blocking():
    job = await swing_tools.claim("backtest", None, {})
    old = (datetime.now(UTC) - swing_tools.STALE_AFTER - timedelta(minutes=1)).isoformat()
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, swing_tools.JOBS_KEY)
        row.value = {"backtest": {**job, "started_at": old}}
        await session.commit()
    assert (await swing_tools.jobs())["backtest"]["status"] == "failed"
    await swing_tools.claim("backfill", None, {})


async def test_starting_a_backtest_queues_it_in_the_background():
    background = BackgroundTasks()
    async with SessionLocal() as session:
        view = await routes.start_backtest(
            routes.BacktestRequest(years=2, strategies=["swing-trend-pullback-v1"]),
            background,
            get_settings(),
            session,
            ADMIN,
        )
    assert view.jobs["backtest"]["status"] == "running"
    assert len(background.tasks) == 1 and background.tasks[0].func is swing_tools.run_backtest_job


async def test_an_unknown_strategy_is_refused():
    async with SessionLocal() as session:
        with pytest.raises(HTTPException):
            await routes.start_backtest(
                routes.BacktestRequest(strategies=["nope"]), BackgroundTasks(), get_settings(), session, ADMIN
            )


# --- the backtest job --------------------------------------------------------------------


async def test_a_backtest_without_enough_history_fails_with_the_reason(monkeypatch):
    async def nothing(session, token, **kwargs):  # noqa: ANN001, ANN003
        return []

    monkeypatch.setattr("app.services.swing_data.load_adjusted_daily", nothing)
    await swing_tools.claim("backtest", None, {})
    await swing_tools.run_backtest_job(get_settings(), 2, ["swing-trend-pullback-v1"])
    job = (await swing_tools.jobs())["backtest"]
    assert job["status"] == "failed" and "fetch daily candles" in job["detail"]


async def test_a_finished_backtest_is_kept_with_the_charges_it_used(monkeypatch):
    from app.services import swing_backtest

    bars = [SimpleNamespace(session_date=date(2025, 1, 1) + timedelta(days=i)) for i in range(300)]

    async def history(session, token, **kwargs):  # noqa: ANN001, ANN003
        return bars

    captured = {}

    def fake_run(universe, sectors, benchmark, *, rules, costs):  # noqa: ANN001
        captured["costs"] = costs
        return swing_backtest.BacktestResult(trades=[], dates=[bar.session_date for bar in bars], skipped={"regime": 3})

    monkeypatch.setattr("app.services.swing_data.load_adjusted_daily", history)
    monkeypatch.setattr(swing_backtest, "run_backtest", fake_run)
    await swing_tools.claim("backtest", None, {})
    await swing_tools.run_backtest_job(get_settings(), 2, ["swing-trend-pullback-v1"])
    assert (await swing_tools.jobs())["backtest"]["status"] == "done"
    runs = await swing_tools.backtest_runs()
    assert len(runs) == 1
    assert runs[0]["charges"]["brokerage_per_order"] == 20
    assert runs[0]["out_of_sample"]["trades"] == 0
    assert runs[0]["skipped"] == {"regime": 3}
    assert captured["costs"].dp_charge_per_sale == Decimal("20")


# --- corporate events ---------------------------------------------------------------------


async def record(**fields):
    async with SessionLocal() as session:
        return await routes.record_event(routes.EventRequest(**fields), session, ADMIN)


async def test_a_demerger_recorded_as_a_split_adjusts_prices_before_its_ex_date():
    await record(symbol=MEMBER.symbol, event_type="SPLIT", ex_date=date(2025, 4, 7), price_factor=2.01, note="Demerger")
    async with SessionLocal() as session:
        events = await confirmed_adjustments(session, MEMBER.instrument_token)
    assert [event.event_type for event in events if event.source == "screen"] == ["SPLIT"]
    before = SimpleNamespace(
        instrument_token="x",
        timeframe_seconds=86400,
        opened_at=None,
        closed_at=None,
        session_date=date(2025, 4, 4),
        open=Decimal("201"),
        high=Decimal("201"),
        low=Decimal("201"),
        close=Decimal("201"),
        volume=100,
        tick_count=0,
    )
    adjusted = adjust_for_events([before], [e for e in events if e.source == "screen"])
    assert adjusted[0].close == Decimal("100")


@pytest.mark.parametrize(
    "fields",
    [
        {"event_type": "SPLIT"},  # no factor
        {"event_type": "RESULTS", "price_factor": 2},  # a factor where none belongs
        {"event_type": "DIVIDEND"},  # no amount
        {"event_type": "MERGER", "price_factor": 2},  # unknown type
        {"event_type": "SPLIT", "price_factor": 2, "symbol": "NOTINDEX"},
    ],
)
async def test_a_wrong_event_is_refused(fields):
    with pytest.raises(HTTPException):
        await record(**{"symbol": MEMBER.symbol, "ex_date": date(2025, 4, 7), **fields})


async def test_the_same_event_twice_is_refused_and_a_recorded_one_can_be_removed():
    view = await record(symbol=MEMBER.symbol, event_type="SPLIT", ex_date=date(2025, 6, 1), price_factor=2)
    with pytest.raises(HTTPException) as duplicate:
        await record(symbol=MEMBER.symbol, event_type="SPLIT", ex_date=date(2025, 6, 1), price_factor=2)
    assert duplicate.value.status_code == 409
    async with SessionLocal() as session:
        await routes.delete_event(view.id, session, ADMIN)
    async with SessionLocal() as session:
        assert await session.get(CorporateEvent, view.id) is None


async def test_the_coverage_report_runs_and_lists_only_shares_that_need_attention():
    report = await routes.data_coverage(get_settings(), ADMIN, years=2)
    assert set(report["counts"]) == {"complete", "gaps", "short", "empty"}
    assert sum(report["counts"].values()) == len(nifty200())
    assert all(share["status"] != "complete" for share in report["shares"])
