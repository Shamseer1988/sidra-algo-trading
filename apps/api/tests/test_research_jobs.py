"""Research Lab Phase 3: research runs in its own worker, never in the API, and cannot trade.

The claims:

* the queue hands each job to exactly one worker, in priority order, and
  records every state change;
* a job whose worker dies is queued again, then failed when out of attempts;
* cancellation and timeouts stop the job's process;
* nothing heavy starts between 9:00 AM and 3:40 PM IST on a trading day;
* the worker refuses to start with broker credentials, and every
  order-sending method raises inside it;
* the API routes enqueue and wait; they run no replay.
"""

import asyncio
import multiprocessing
import os
import pathlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app import research_worker
from app.db.models import AuditLog, BacktestRun, ResearchJob, ResearchJobEvent, User, UserRole
from app.db.session import SessionLocal
from app.services import research_jobs, research_runs, user_admin
from app.services.trading_calendar import TradingCalendar

# The API folder is the root that exists everywhere: in a checkout it is
# apps/api, and inside the test container scripts/run-tests.sh builds, it is
# /app (only apps/api is copied in). The repository root, and with it
# docker-compose.yml, exists only in a checkout.
API_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = API_ROOT.parents[1] if (API_ROOT.parents[1] / "docker-compose.yml").exists() else None
NO_CHECKOUT = "needs the repository checkout (docker-compose.yml); the test container holds apps/api only"
IST_OFFSET = timedelta(hours=5, minutes=30)
# What TradingCalendar.from_settings reads; the routes build the research window from it.
CALENDAR_SETTINGS = SimpleNamespace(nse_holiday_overrides="", nse_special_sessions="", calendar_confirmed_years={2026})


def _ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - IST_OFFSET


@pytest.fixture
async def clean_jobs():
    """Every test starts with an empty queue and leaves one."""

    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(ResearchJobEvent))
            await session.execute(delete(ResearchJob))
            await session.commit()

    await wipe()
    yield
    await wipe()


async def _enqueue(job_type="backtest", payload=None, *, priority=0, max_attempts=2, timeout_seconds=60):
    async with SessionLocal() as session:
        job = await research_jobs.enqueue(
            session,
            job_type,
            payload or {},
            user_id=None,
            priority=priority,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        await session.commit()
        return job.id


async def _get(job_id):
    async with SessionLocal() as session:
        job = await session.get(ResearchJob, job_id)
        events = (
            await session.scalars(
                select(ResearchJobEvent).where(ResearchJobEvent.job_id == job_id).order_by(ResearchJobEvent.occurred_at)
            )
        ).all()
        return job, [(e.from_status, e.to_status, e.actor) for e in events]


# --- the queue ------------------------------------------------------------------------


async def test_enqueue_records_the_job_and_who_asked(clean_jobs):
    job_id = await _enqueue(payload={"x": 1})
    job, events = await _get(job_id)
    assert job.status == research_jobs.QUEUED and job.payload == {"x": 1} and job.attempts == 0
    assert events == [("", "QUEUED", "system")]


async def test_claim_takes_the_highest_priority_then_the_oldest(clean_jobs):
    first = await _enqueue()
    urgent = await _enqueue(priority=5)
    second = await _enqueue()
    order = []
    for _ in range(4):
        async with SessionLocal() as session:
            job = await research_jobs.claim_next(session, "w1")
            order.append(job.id if job else None)
            await session.commit()
    assert order == [urgent, first, second, None]


async def test_two_workers_never_take_the_same_job(clean_jobs):
    one, two = await _enqueue(), await _enqueue()
    async with SessionLocal() as a, SessionLocal() as b:
        first = await research_jobs.claim_next(a, "wa")  # row locked, not yet committed
        second = await research_jobs.claim_next(b, "wb")  # skips the locked row
        assert {first.id, second.id} == {one, two}
        await a.commit()
        await b.commit()


async def test_heartbeat_reports_a_cancel_request(clean_jobs):
    job_id = await _enqueue()
    async with SessionLocal() as session:
        await research_jobs.claim_next(session, "w1")
        await session.commit()
    async with SessionLocal() as session:
        assert await research_jobs.heartbeat(session, job_id, "w1") is False
        await research_jobs.request_cancel(session, job_id, actor="user:test")
        await session.commit()
    async with SessionLocal() as session:
        assert await research_jobs.heartbeat(session, job_id, "w1") is True
        assert await research_jobs.heartbeat(session, job_id, "someone-else") is False


async def test_cancelling_a_queued_job_ends_it_at_once(clean_jobs):
    job_id = await _enqueue()
    async with SessionLocal() as session:
        await research_jobs.request_cancel(session, job_id, actor="user:test")
        await session.commit()
    job, events = await _get(job_id)
    assert job.status == research_jobs.CANCELLED and events[-1][1:] == ("CANCELLED", "user:test")


async def test_the_first_finish_stands(clean_jobs):
    job_id = await _enqueue()
    async with SessionLocal() as session:
        await research_jobs.finish(session, job_id, research_jobs.FAILED, actor="a", detail="first")
        await research_jobs.finish(session, job_id, research_jobs.SUCCEEDED, actor="b", result={"x": 1})
        await session.commit()
    job, _ = await _get(job_id)
    assert job.status == research_jobs.FAILED and job.failure_detail == "first" and job.result is None


async def test_a_dead_worker_s_job_is_queued_again_then_failed(clean_jobs):
    job_id = await _enqueue(max_attempts=2)
    later = datetime.now(UTC) + research_jobs.STALE_AFTER + timedelta(seconds=5)
    for expected in (research_jobs.QUEUED, research_jobs.FAILED):
        async with SessionLocal() as session:
            await research_jobs.claim_next(session, "w1")
            await session.commit()
        async with SessionLocal() as session:
            assert job_id in await research_jobs.requeue_stale(session, now=later)
            await session.commit()
        job, _ = await _get(job_id)
        assert job.status == expected
    assert job.attempts == 2 and "heartbeat stopped" in job.failure_detail


async def test_a_stopping_worker_puts_its_job_back(clean_jobs):
    job_id = await _enqueue()
    async with SessionLocal() as session:
        await research_jobs.claim_next(session, "w1")
        await session.commit()
    async with SessionLocal() as session:
        await research_jobs.release(session, job_id, actor="w1", detail="stopping")
        await session.commit()
    job, _ = await _get(job_id)
    assert job.status == research_jobs.QUEUED and job.worker_id is None


async def test_wait_for_returns_on_finish_or_gives_up(clean_jobs):
    job_id = await _enqueue()
    waited = await research_jobs.wait_for(job_id, timeout_seconds=0.3, poll_seconds=0.1)
    assert waited.status == research_jobs.QUEUED

    async def finish_soon():
        await asyncio.sleep(0.2)
        async with SessionLocal() as session:
            await research_jobs.finish(session, job_id, research_jobs.SUCCEEDED, actor="t", result={"ok": True})
            await session.commit()

    done, _ = await asyncio.gather(research_jobs.wait_for(job_id, timeout_seconds=5, poll_seconds=0.05), finish_soon())
    assert done.status == research_jobs.SUCCEEDED and done.result == {"ok": True}


# --- the research window ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "is_open"),
    [
        (_ist(date(2026, 11, 27), 8, 59), True),  # Friday, before the window
        (_ist(date(2026, 11, 27), 9, 0), False),
        (_ist(date(2026, 11, 27), 12, 0), False),
        (_ist(date(2026, 11, 27), 15, 39), False),
        (_ist(date(2026, 11, 27), 15, 40), True),
        (_ist(date(2026, 11, 28), 12, 0), True),  # Saturday
        (_ist(date(2026, 12, 25), 12, 0), True),  # Christmas, an exchange holiday
    ],
)
def test_heavy_research_waits_for_market_hours_to_end(moment, is_open):
    assert research_jobs.research_allowed(moment, TradingCalendar()).open is is_open


# --- the worker cannot trade --------------------------------------------------------------


def test_the_worker_refuses_broker_credentials_and_names_only_the_variables():
    research_worker.refuse_broker_credentials({"DATABASE_URL": "x", "UPSTOX_PIN": ""})  # blank is fine
    with pytest.raises(research_worker.ResearchWorkerRefused) as refused:
        research_worker.refuse_broker_credentials({"UPSTOX_TOTP_SECRET": "SEED123", "TELEGRAM_BOT_TOKEN": "tok"})
    message = str(refused.value)
    assert "UPSTOX_TOTP_SECRET" in message and "TELEGRAM_BOT_TOKEN" in message
    assert "SEED123" not in message and "tok" not in message.replace("TELEGRAM_BOT_TOKEN", "")


async def test_every_order_path_raises_inside_the_worker():
    """The gateway's factory and the two send functions, and every module's imported copy of them."""
    import importlib
    import sys

    from app.services import live_entry_bridge, live_exit_manager  # noqa: F401 - their copies must be replaced too

    saved = []
    for module_name, attribute in research_worker.ORDER_PATHS:
        original = getattr(importlib.import_module(module_name), attribute)
        for module in list(sys.modules.values()):
            if module is not None and getattr(module, attribute, None) is original:
                saved.append((module, attribute, original))
    try:
        disabled = research_worker.disable_order_paths()
        assert "app.services.live_entry_bridge.live_order_adapter" in disabled
        assert "app.services.live_exit_manager.live_order_adapter" in disabled
        assert len(disabled) == len(saved)
        for module, attribute, _ in saved:
            with pytest.raises(research_worker.OrderPathDisabled):
                await getattr(module, attribute)(object())
    finally:
        for module, attribute, original in saved:
            setattr(module, attribute, original)


def test_the_worker_names_no_order_capable_client():
    source = (API_ROOT / "app/research_worker.py").read_text(encoding="utf-8")
    assert "UpstoxOrderClient" not in source and "FirstockOrderClient" not in source


@pytest.mark.skipif(REPO is None, reason=NO_CHECKOUT)
def test_compose_gives_the_worker_no_credentials_limits_and_no_ports():
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("  research-worker:\n", 1)[1].split("\n  web:\n", 1)[0]
    for name in research_worker.FORBIDDEN_ENVIRONMENT:
        assert f'      {name}: ""' in block, name
    assert "cpus:" in block and "mem_limit:" in block
    # edge carries outbound HTTPS to the public history endpoint (Phase 4); no port is published.
    assert "networks: [internal, edge]" in block
    assert "ports:" not in block
    assert "mem_limit: 1g" in block


# --- supervising a job ------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, *, runs_for: float, exitcode: int = 0, on_start=None):
        self.runs_for, self.exitcode, self.on_start = runs_for, exitcode, on_start
        self.started = None
        self.terminated = False

    def start(self):
        self.started = asyncio.get_event_loop().time()

    def is_alive(self):
        if self.terminated:
            return False
        return asyncio.get_event_loop().time() - self.started < self.runs_for

    def terminate(self):
        self.terminated = True
        self.exitcode = -15

    def join(self, _timeout=None):
        return None


async def _claimed(**kwargs):
    job_id = await _enqueue(**kwargs)
    async with SessionLocal() as session:
        await research_jobs.claim_next(session, "w1")
        await session.commit()
    return job_id


async def test_a_job_past_its_timeout_is_stopped_and_failed(clean_jobs, monkeypatch):
    monkeypatch.setattr(research_worker, "JOB_HEARTBEAT_SECONDS", 0)
    job_id = await _claimed(timeout_seconds=1)
    process = FakeProcess(runs_for=60)
    status = await research_worker.supervise(job_id, 1, "w1", process_factory=lambda: process)
    job, _ = await _get(job_id)
    assert process.terminated and status == research_jobs.FAILED and "Timed out" in job.failure_detail


async def test_a_cancelled_running_job_is_stopped(clean_jobs, monkeypatch):
    monkeypatch.setattr(research_worker, "JOB_HEARTBEAT_SECONDS", 0)
    job_id = await _claimed()
    async with SessionLocal() as session:
        await research_jobs.request_cancel(session, job_id, actor="user:test")
        await session.commit()
    process = FakeProcess(runs_for=60)
    status = await research_worker.supervise(job_id, 60, "w1", process_factory=lambda: process)
    assert process.terminated and status == research_jobs.CANCELLED


async def test_a_child_that_dies_without_a_result_fails_the_job(clean_jobs):
    job_id = await _claimed()
    status = await research_worker.supervise(
        job_id, 60, "w1", process_factory=lambda: FakeProcess(runs_for=0.6, exitcode=137)
    )
    job, _ = await _get(job_id)
    assert status == research_jobs.FAILED and "exit code 137" in job.failure_detail


# --- the child records its own outcome ----------------------------------------------------------


async def test_the_child_records_success_and_input_errors(clean_jobs, monkeypatch):
    async def ok(session, payload, settings, user_id):
        return {"echo": payload["n"]}

    async def bad(session, payload, settings, user_id):
        raise research_runs.ResearchInputError("No requested historical candles are available")

    monkeypatch.setitem(research_runs.HANDLERS, "test-ok", ok)
    monkeypatch.setitem(research_runs.HANDLERS, "test-bad", bad)
    good, broken, unknown = (
        await _claimed(job_type="test-ok", payload={"n": 7}),
        await _claimed(job_type="test-bad"),
        await _claimed(job_type="no-such-type"),
    )
    assert await research_worker._child_main(good) == 0
    assert await research_worker._child_main(broken) == 0
    assert await research_worker._child_main(unknown) == 2
    assert (await _get(good))[0].result == {"echo": 7}
    assert (await _get(good))[0].status == research_jobs.SUCCEEDED
    failed = (await _get(broken))[0]
    assert failed.status == research_jobs.FAILED and "historical candles" in failed.failure_detail
    assert (await _get(unknown))[0].status == research_jobs.FAILED


async def test_a_real_child_process_runs_and_reports(clean_jobs, monkeypatch):
    """Across a real process boundary: spawn, lock down, run, record. An unknown type fails cleanly."""
    monkeypatch.setattr(research_worker, "JOB_HEARTBEAT_SECONDS", 0.2)
    job_id = await _claimed(job_type="no-such-type")
    context = multiprocessing.get_context("spawn")
    env_backup = {name: os.environ.pop(name) for name in research_worker.FORBIDDEN_ENVIRONMENT if name in os.environ}
    try:
        status = await research_worker.supervise(
            job_id,
            120,
            "w1",
            process_factory=lambda: context.Process(target=research_worker.run_in_child, args=(str(job_id),)),
        )
    finally:
        os.environ.update(env_backup)
    job, _ = await _get(job_id)
    assert status == research_jobs.FAILED and "Unknown job type" in job.failure_detail


# --- the API waits; it does not run -------------------------------------------------------------


@pytest.fixture
async def admin():
    async with SessionLocal() as session:
        user = await user_admin.create_user(
            session, f"research-{uuid4().hex[:8]}@test.invalid", "correct horse battery", UserRole.ADMIN
        )
        await session.commit()
        await session.refresh(user)
    yield user
    async with SessionLocal() as session:
        await session.execute(delete(BacktestRun).where(BacktestRun.created_by_user_id == user.id))
        await session.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
        await session.execute(delete(ResearchJobEvent))
        await session.execute(delete(ResearchJob).where(ResearchJob.created_by_user_id == user.id))
        await session.flush()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


def _request():
    from app.api.routes.backtesting import BacktestRunRequest

    return BacktestRunRequest(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6), instrument_tokens=["NSE:A"])


async def test_inside_market_hours_the_route_queues_and_says_so(clean_jobs, admin, monkeypatch):
    from app.api.routes import backtesting as routes
    from app.services.strategy_registry import StrategyConfiguration

    monkeypatch.setattr(routes.research_jobs, "research_allowed", lambda *_: research_jobs.Gate(False, "Market hours."))
    strategy = StrategyConfiguration(id="s", name="Strategy s", strategy_type="rs-pullback-v1")
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.execute_backtest(
                _request(),
                CALENDAR_SETTINGS,
                session,
                admin,
                [strategy],
                {"account_capital": 50000},
                routes.PaperExecutionControls(),
            )
    assert refused.value.status_code == 409 and "Queued as research job" in refused.value.detail
    async with SessionLocal() as session:
        queued = (await session.scalars(select(ResearchJob))).all()
    assert len(queued) == 1 and queued[0].status == research_jobs.QUEUED and queued[0].job_type == "backtest"
    assert queued[0].payload["strategies"][0]["id"] == "s"


async def test_outside_market_hours_the_route_returns_the_worker_s_result(clean_jobs, admin, monkeypatch):
    from app.api.routes import backtesting as routes
    from app.services.strategy_registry import StrategyConfiguration

    async def fake_backtest(session, payload, settings, user_id):
        run = BacktestRun(
            created_by_user_id=user_id,
            status="COMPLETED",
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 6),
            timeframe_seconds=60,
            instrument_tokens=["NSE:A"],
            data_fingerprint="0" * 64,
            initial_capital=50000,
        )
        session.add(run)
        await session.flush()
        return {"backtest_run_id": str(run.id), "trades": 0}

    async def worker_does_it(job_id, **_kwargs):
        # Stands in for the research-worker container: the same child entry point.
        async with SessionLocal() as session:
            await research_jobs.claim_next(session, "test-worker")
            await session.commit()
        await research_worker._child_main(job_id)
        async with SessionLocal() as session:
            job = await session.get(ResearchJob, job_id)
            session.expunge(job)
            return job

    monkeypatch.setitem(research_runs.HANDLERS, research_runs.BACKTEST, fake_backtest)
    monkeypatch.setattr(routes.research_jobs, "research_allowed", lambda *_: research_jobs.Gate(True, "Open."))
    monkeypatch.setattr(routes.research_jobs, "wait_for", worker_does_it)
    strategy = StrategyConfiguration(id="s", name="Strategy s", strategy_type="rs-pullback-v1")
    async with SessionLocal() as session:
        detail = await routes.execute_backtest(
            _request(),
            CALENDAR_SETTINGS,
            session,
            admin,
            [strategy],
            {"account_capital": 50000},
            routes.PaperExecutionControls(),
        )
    assert detail.status == "COMPLETED" and detail.trades == []


def test_the_routes_no_longer_run_a_replay():
    source = (API_ROOT / "app/api/routes/backtesting.py").read_text(encoding="utf-8")
    for call in ("run_portfolio_backtest(", "run_parameter_sweep(", "run_completed_candle_backtest(", "to_thread("):
        assert call not in source, call
    assert "research_jobs.enqueue(" in source


def test_only_an_administrator_may_cancel():
    from app.api.routes import research as routes

    route = next(item for item in routes.router.routes if getattr(item, "path", "") == "/research/jobs/{job_id}/cancel")
    dependency = next(dep for dep in route.dependant.dependencies if getattr(dep, "name", None) == "user")
    viewer = User(id=uuid4(), email="v@test.invalid", password_hash="x", role=UserRole.VIEWER, is_active=True)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(dependency.call(viewer))
    assert refused.value.status_code == 403
