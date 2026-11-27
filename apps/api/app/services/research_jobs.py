"""The research job queue: how the API asks for a backtest, and how the worker takes one.

Postgres is the queue. A job is a row; a worker claims the oldest queued one
with ``FOR UPDATE SKIP LOCKED``, so two workers can never take the same job and
a claim never waits on another's lock. Every state change also writes a
``research_job_events`` row naming who made it.

Status: QUEUED -> RUNNING -> SUCCEEDED | FAILED | CANCELLED.

* A RUNNING job whose heartbeat stops (the worker died, the container was
  recreated) goes back to QUEUED while it has attempts left, then FAILED.
* Cancelling a QUEUED job ends it at once. Cancelling a RUNNING one sets a flag
  the worker reads between heartbeats; the worker then stops the job's process.

**Research runs outside market hours.** ``research_allowed`` is closed from
9:00 AM to 3:40 PM IST on trading days. A sweep loads hundreds of megabytes and
holds a CPU for an hour; on the NAS that competes with the scanner for the
memory and CPU it needs to place and protect orders. Jobs may still be queued
during the day; they wait.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchJob, ResearchJobEvent
from app.services.trading_calendar import MARKET_TIMEZONE, TradingCalendar

QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
FINISHED = frozenset({SUCCEEDED, FAILED, CANCELLED})

# Closed window on trading days, IST. Starts before the 9:15 open so the morning
# backfill and pre-open checks have the machine; ends after the 3:30 close and
# the 3:40 deploy cutoff the sessions already observe.
RESEARCH_CLOSED_FROM = time(9, 0)
RESEARCH_CLOSED_UNTIL = time(15, 40)

# A heartbeat older than this means the worker is gone.
STALE_AFTER = timedelta(minutes=3)

DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


class JobNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Gate:
    open: bool
    reason: str


def research_allowed(now: datetime, calendar: TradingCalendar) -> Gate:
    """Whether heavy research may run now. Weekends and exchange holidays are open all day."""
    local = now.astimezone(MARKET_TIMEZONE)
    status = calendar.status_at(now)
    if not status.trading_day:
        return Gate(True, f"Not a trading day ({status.reason}).")
    if RESEARCH_CLOSED_FROM <= local.time().replace(tzinfo=None) < RESEARCH_CLOSED_UNTIL:
        return Gate(
            False,
            "Research runs only outside market hours: before 9:00 AM or after 3:40 PM IST on a trading day.",
        )
    return Gate(True, "Outside market hours.")


def _event(job: ResearchJob, to_status: str, actor: str, detail: str | None = None, *, from_status: str | None = None):
    return ResearchJobEvent(
        job_id=job.id,
        from_status=from_status if from_status is not None else job.status,
        to_status=to_status,
        actor=actor[:80],
        detail=(detail or None) and detail[:500],
    )


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict,
    *,
    user_id: UUID | None,
    priority: int = 0,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = 2,
) -> ResearchJob:
    job = ResearchJob(
        job_type=job_type,
        status=QUEUED,
        payload=payload,
        created_by_user_id=user_id,
        priority=priority,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    session.add(_event(job, QUEUED, f"user:{user_id}" if user_id else "system", from_status=""))
    await session.flush()
    return job


async def claim_next(session: AsyncSession, worker_id: str, *, now: datetime | None = None) -> ResearchJob | None:
    """Take the next queued job for this worker, or None. Commit to release the row lock."""
    now = now or datetime.now(UTC)
    job = await session.scalar(
        select(ResearchJob)
        .where(ResearchJob.status == QUEUED, ResearchJob.cancel_requested.is_(False))
        .order_by(ResearchJob.priority.desc(), ResearchJob.created_at, ResearchJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    session.add(_event(job, RUNNING, f"worker:{worker_id}", f"attempt {job.attempts + 1} of {job.max_attempts}"))
    job.status = RUNNING
    job.attempts += 1
    job.worker_id = worker_id
    job.started_at = now
    job.heartbeat_at = now
    await session.flush()
    return job


async def heartbeat(session: AsyncSession, job_id: UUID, worker_id: str, *, now: datetime | None = None) -> bool:
    """Record that the job is alive. Returns whether cancellation has been asked for."""
    job = await session.get(ResearchJob, job_id)
    if job is None or job.status != RUNNING or job.worker_id != worker_id:
        return False
    job.heartbeat_at = now or datetime.now(UTC)
    await session.flush()
    return bool(job.cancel_requested)


async def finish(
    session: AsyncSession,
    job_id: UUID,
    status: str,
    *,
    actor: str,
    result: dict | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> ResearchJob:
    if status not in FINISHED:
        raise ValueError(f"{status} is not a finishing status")
    job = await session.get(ResearchJob, job_id, with_for_update=True)
    if job is None:
        raise JobNotFound(str(job_id))
    if job.status in FINISHED:
        return job  # already settled; the first word stands
    session.add(_event(job, status, actor, detail))
    job.status = status
    job.result = result
    job.failure_detail = (detail or None) and detail[:500]
    job.finished_at = now or datetime.now(UTC)
    await session.flush()
    return job


async def request_cancel(session: AsyncSession, job_id: UUID, *, actor: str) -> ResearchJob:
    job = await session.get(ResearchJob, job_id, with_for_update=True)
    if job is None:
        raise JobNotFound(str(job_id))
    if job.status == QUEUED:
        return await finish(session, job_id, CANCELLED, actor=actor, detail="Cancelled before it started")
    if job.status == RUNNING and not job.cancel_requested:
        job.cancel_requested = True
        session.add(_event(job, RUNNING, actor, "Cancellation requested"))
        await session.flush()
    return job


async def release(
    session: AsyncSession, job_id: UUID, *, actor: str, detail: str, refund_attempt: bool = False
) -> ResearchJob | None:
    """Put a RUNNING job back in the queue because its worker is stopping, not because it failed.

    ``refund_attempt`` gives back the attempt the claim used, for a job stopped
    on purpose -- the research window closing -- so a long job that is paused
    every morning does not run out of attempts.
    """
    job = await session.get(ResearchJob, job_id, with_for_update=True)
    if job is None or job.status != RUNNING:
        return job
    if job.cancel_requested:
        return await finish(session, job_id, CANCELLED, actor=actor, detail="Worker stopped after cancellation")
    session.add(_event(job, QUEUED, actor, detail))
    job.status = QUEUED
    job.worker_id = None
    if refund_attempt and job.attempts > 0:
        job.attempts -= 1
    await session.flush()
    return job


async def requeue_stale(session: AsyncSession, *, now: datetime | None = None) -> list[UUID]:
    """Return orphaned RUNNING jobs to the queue, or fail them when out of attempts."""
    now = now or datetime.now(UTC)
    stale = list(
        (
            await session.scalars(
                select(ResearchJob)
                .where(ResearchJob.status == RUNNING, ResearchJob.heartbeat_at < now - STALE_AFTER)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for job in stale:
        if job.cancel_requested:
            await finish(session, job.id, CANCELLED, actor="supervisor", detail="Worker stopped after cancellation")
        elif job.attempts < job.max_attempts:
            session.add(_event(job, QUEUED, "supervisor", "Worker heartbeat stopped; queued again"))
            job.status = QUEUED
            job.worker_id = None
        else:
            await finish(
                session, job.id, FAILED, actor="supervisor", detail=f"Worker heartbeat stopped {job.attempts} times"
            )
    await session.flush()
    return [job.id for job in stale]


async def wait_for(
    job_id: UUID,
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    session_factory: Any = None,
) -> ResearchJob | None:
    """Poll until the job finishes or the wait runs out. Returns the job as last seen.

    Uses short sessions of its own, so a waiting request holds no database
    connection between polls.
    """
    if session_factory is None:
        from app.db.session import SessionLocal

        session_factory = SessionLocal
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    job = None
    while True:
        async with session_factory() as session:
            job = await session.get(ResearchJob, job_id)
            if job is not None:
                session.expunge(job)
        if job is None or job.status in FINISHED or asyncio.get_running_loop().time() >= deadline:
            return job
        await asyncio.sleep(poll_seconds)
