"""Research worker: runs backtests and sweeps in their own container, never in the API.

    python -m app.research_worker

One job at a time. Each job runs in a child process, so a job that hangs, runs
past its timeout, or is cancelled can be stopped outright -- a thread running a
CPU-bound replay cannot be interrupted, a process can. The parent keeps the
job's heartbeat, watches for a cancel request and the timeout, and records the
outcome if the child dies without recording one itself.

**It cannot trade.** Research code reaches the broker modules transitively
(``backtesting`` -> ``paper_execution`` -> ``live_shadow_runner`` ->
``upstox_orders``); cutting that edge means changing the paper engine, which is
not this module's to change. So three independent locks instead:

1. The container is started without broker or Telegram credentials -- the
   Compose file blanks them, including the key that decrypts the stored Upstox
   token, so no token can be read from the database either.
2. ``refuse_broker_credentials`` stops the process at startup if any of them is
   present anyway.
3. ``disable_order_paths`` replaces the one factory that builds an
   order-capable broker client, and the two functions that send, with
   functions that raise -- in this process and in each child.

It also never runs heavy work between 9:00 AM and 3:40 PM IST on a trading day
(``research_jobs.research_allowed``), when the scanner needs the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import socket
import sys
import time
from datetime import UTC, datetime
from uuid import UUID

import structlog

# Names only; a value is never read except to test that it is empty.
FORBIDDEN_ENVIRONMENT = (
    "UPSTOX_ACCESS_TOKEN",
    "UPSTOX_API_KEY",
    "UPSTOX_API_SECRET",
    "UPSTOX_PIN",
    "UPSTOX_TOTP_SECRET",
    "UPSTOX_MOBILE_NUMBER",
    "UPSTOX_TOKEN_ENCRYPTION_KEY",
    "FIRSTOCK_API_KEY",
    "FIRSTOCK_PASSWORD",
    "FIRSTOCK_TOTP_SECRET",
    "FIRSTOCK_USER_ID",
    "FIRSTOCK_VENDOR_CODE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CONFIG_ENCRYPTION_KEY",
)

from app.services.research_worker_status import WORKER_HEARTBEAT_KEY  # noqa: E402

POLL_SECONDS = 5.0
CLOSED_POLL_SECONDS = 30.0
JOB_HEARTBEAT_SECONDS = 10.0


class ResearchWorkerRefused(SystemExit):
    pass


def refuse_broker_credentials(environ: dict[str, str] | None = None) -> None:
    """Exit if this process could authenticate to a broker or Telegram. Names only in the message."""
    env = os.environ if environ is None else environ
    present = [name for name in FORBIDDEN_ENVIRONMENT if (env.get(name) or "").strip()]
    if present:
        raise ResearchWorkerRefused(
            "research-worker refuses to start: broker or Telegram credentials are set ("
            + ", ".join(present)
            + "). Blank them for this service in docker-compose.yml."
        )


class OrderPathDisabled(RuntimeError):
    pass


def _refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
    raise OrderPathDisabled("The research worker cannot place, cancel or modify orders.")


async def _refuse_async(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
    _refuse()


# The functions through which any order can be sent. Only
# ``live_execution_gateway.live_order_adapter`` may construct an order-capable
# broker client -- ``tests/test_submission_boundary.py`` enforces that -- so
# disabling it, and the two functions that send, closes every path without this
# module naming a client class (which that test would rightly refuse).
ORDER_PATHS = (
    ("app.services.live_execution_gateway", "live_order_adapter"),
    ("app.services.live_orders", "send_prepared_order"),
    ("app.services.live_execution", "submit_live_order"),
)


def disable_order_paths() -> list[str]:
    """Replace every order-sending entry point with one that raises, here and wherever it was imported."""
    import importlib

    disabled: list[str] = []
    for module_name, attribute in ORDER_PATHS:
        module = importlib.import_module(module_name)
        original = getattr(module, attribute)
        setattr(module, attribute, _refuse_async)
        disabled.append(f"{module_name}.{attribute}")
        # ``from module import name`` copies the reference; replace the copies too.
        for other in list(sys.modules.values()):
            if other is not None and getattr(other, attribute, None) is original:
                setattr(other, attribute, _refuse_async)
                disabled.append(f"{other.__name__}.{attribute}")
    return disabled


def _lock_down() -> None:
    refuse_broker_credentials()
    disable_order_paths()


# --- the child: one job ------------------------------------------------------------


def run_in_child(job_id: str) -> None:
    """Child-process entry point. Records the job's outcome itself."""
    _lock_down()
    code = asyncio.run(_child_main(UUID(job_id)))
    sys.exit(code)


async def _child_main(job_id: UUID) -> int:
    from app.core.config import get_settings
    from app.db.models import ResearchJob
    from app.db.session import SessionLocal
    from app.services import research_jobs
    from app.services.research_runs import HANDLERS, ResearchInputError

    settings = get_settings()
    actor = f"worker:{socket.gethostname()}:{os.getpid()}"
    async with SessionLocal() as session:
        job = await session.get(ResearchJob, job_id)
        if job is None:
            return 2
        handler = HANDLERS.get(job.job_type)
        if handler is None:
            await research_jobs.finish(
                session, job_id, research_jobs.FAILED, actor=actor, detail=f"Unknown job type {job.job_type!r}"
            )
            await session.commit()
            return 2
        payload, user_id = job.payload, job.created_by_user_id
        try:
            result = await handler(session, payload, settings, user_id)
        except ResearchInputError as exc:
            await session.rollback()
            await research_jobs.finish(session, job_id, research_jobs.FAILED, actor=actor, detail=str(exc))
            await session.commit()
            return 0
        except Exception as exc:  # noqa: BLE001 - recorded on the job, then the process ends
            await session.rollback()
            await research_jobs.finish(
                session, job_id, research_jobs.FAILED, actor=actor, detail=f"{type(exc).__name__}: {exc}"
            )
            await session.commit()
            return 1
        # The rows the job wrote and its SUCCEEDED status commit together.
        await research_jobs.finish(session, job_id, research_jobs.SUCCEEDED, actor=actor, result=result)
        await session.commit()
    return 0


# --- the parent: supervise one job ------------------------------------------------------


async def supervise(  # noqa: PLR0912, PLR0915 - one loop, every way a job can end
    job_id: UUID,
    timeout_seconds: int,
    worker_id: str,
    *,
    process_factory=None,  # noqa: ANN001
    window_open=None,  # noqa: ANN001 - Callable[[], bool]
) -> str:
    """Run the job in a child process; keep its heartbeat; stop it on cancel or timeout.

    Returns the status the job ended in, as recorded.
    """
    from app.db.session import SessionLocal
    from app.services import research_jobs

    context = multiprocessing.get_context("spawn")
    factory = process_factory or (lambda: context.Process(target=run_in_child, args=(str(job_id),), daemon=False))
    process = factory()
    process.start()
    started = time.monotonic()
    last_beat = 0.0
    stop_reason: tuple[str, str] | None = None
    try:
        while process.is_alive():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - last_beat >= JOB_HEARTBEAT_SECONDS:
                last_beat = now
                async with SessionLocal() as session:
                    cancel = await research_jobs.heartbeat(session, job_id, worker_id)
                    await session.commit()
                if cancel:
                    stop_reason = (research_jobs.CANCELLED, "Cancelled while running")
            if stop_reason is None and now - started > timeout_seconds:
                stop_reason = (research_jobs.FAILED, f"Timed out after {timeout_seconds} seconds")
            if stop_reason is None and window_open is not None and not window_open():
                # The research window closed with the job still running: the
                # scanner needs the machine. Stop it and queue it again without
                # spending an attempt; a backfill resumes from its last file.
                process.terminate()
                process.join(10)
                async with SessionLocal() as session:
                    await research_jobs.release(
                        session,
                        job_id,
                        actor=worker_id,
                        detail="Research window closed; queued again",
                        refund_attempt=True,
                    )
                    await session.commit()
                return research_jobs.QUEUED
            if stop_reason is not None:
                process.terminate()
                process.join(10)
                break
    except asyncio.CancelledError:
        # The worker itself is stopping. Stop the job and put it back.
        process.terminate()
        process.join(10)
        async with SessionLocal() as session:
            await research_jobs.release(session, job_id, actor=worker_id, detail="Worker stopped; queued again")
            await session.commit()
        raise

    process.join(1)
    async with SessionLocal() as session:
        if stop_reason is not None:
            job = await research_jobs.finish(session, job_id, stop_reason[0], actor=worker_id, detail=stop_reason[1])
        else:
            from app.db.models import ResearchJob

            job = await session.get(ResearchJob, job_id)
            if job is not None and job.status not in research_jobs.FINISHED:
                job = await research_jobs.finish(
                    session,
                    job_id,
                    research_jobs.FAILED,
                    actor=worker_id,
                    detail=f"Job process ended with exit code {process.exitcode} without recording a result",
                )
        await session.commit()
        return job.status if job is not None else research_jobs.FAILED


# --- the loop -----------------------------------------------------------------------------


async def run(*, once: bool = False) -> None:
    from redis.asyncio import Redis

    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.db.session import SessionLocal
    from app.services import research_jobs
    from app.services.trading_calendar import TradingCalendar

    _lock_down()
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger("research_worker")
    calendar = TradingCalendar.from_settings(settings)
    worker_id = f"research:{socket.gethostname()}:{os.getpid()}"
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    logger.info("research_worker.started", worker_id=worker_id)
    try:
        while True:
            now = datetime.now(UTC)
            gate = research_jobs.research_allowed(now, calendar)
            with contextlib.suppress(Exception):
                await redis.set(
                    WORKER_HEARTBEAT_KEY,
                    f"{now.isoformat()}|{'open' if gate.open else 'closed'}|{worker_id}",
                    ex=120,
                )
            async with SessionLocal() as session:
                requeued = await research_jobs.requeue_stale(session)
                await session.commit()
            if requeued:
                logger.warning("research_worker.requeued_stale", jobs=[str(item) for item in requeued])
            if not gate.open:
                if once:
                    return
                await asyncio.sleep(CLOSED_POLL_SECONDS)
                continue
            async with SessionLocal() as session:
                job = await research_jobs.claim_next(session, worker_id)
                claimed = (job.id, job.timeout_seconds, job.job_type) if job is not None else None
                await session.commit()
            if claimed is None:
                if once:
                    return
                await asyncio.sleep(POLL_SECONDS)
                continue
            job_id, timeout_seconds, job_type = claimed
            logger.info("research_worker.job_started", job_id=str(job_id), job_type=job_type)
            outcome = await supervise(
                job_id,
                timeout_seconds,
                worker_id,
                window_open=lambda: research_jobs.research_allowed(datetime.now(UTC), calendar).open,
            )
            logger.info("research_worker.job_finished", job_id=str(job_id), status=outcome)
            if once:
                return
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
