"""Research jobs: what is queued, running and finished, and how to stop one.

Read by anyone signed in; cancelling needs an administrator. These routes run
nothing themselves -- the research worker does.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import ResearchJob, ResearchJobEvent, User, UserRole
from app.services import research_jobs
from app.services.research_worker_status import WORKER_HEARTBEAT_KEY, parse_heartbeat
from app.services.trading_calendar import TradingCalendar

router = APIRouter(prefix="/research", tags=["Research"])


class ResearchJobResponse(BaseModel):
    id: UUID
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    cancel_requested: bool
    result: dict | None
    failure_detail: str | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None


class ResearchJobEventResponse(BaseModel):
    from_status: str | None
    to_status: str
    actor: str
    detail: str | None
    occurred_at: datetime


class ResearchJobDetail(ResearchJobResponse):
    events: list[ResearchJobEventResponse]


class ResearchWorkerStatus(BaseModel):
    status: str
    detail: str
    last_seen: datetime | None
    research_window_open: bool
    window_detail: str
    queued: int
    running: int


def _job(row: ResearchJob) -> ResearchJobResponse:
    return ResearchJobResponse(
        id=row.id,
        job_type=row.job_type,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        cancel_requested=row.cancel_requested,
        result=row.result,
        failure_detail=row.failure_detail,
        worker_id=row.worker_id,
        created_at=row.created_at,
        started_at=row.started_at,
        heartbeat_at=row.heartbeat_at,
        finished_at=row.finished_at,
    )


@router.get("/jobs", response_model=list[ResearchJobResponse])
async def list_jobs(
    _: CurrentUser, session: DbSession, limit: int = Query(default=30, ge=1, le=200)
) -> list[ResearchJobResponse]:
    rows = (await session.scalars(select(ResearchJob).order_by(ResearchJob.created_at.desc()).limit(limit))).all()
    return [_job(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=ResearchJobDetail)
async def get_job(job_id: UUID, _: CurrentUser, session: DbSession) -> ResearchJobDetail:
    row = await session.get(ResearchJob, job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found")
    events = (
        await session.scalars(
            select(ResearchJobEvent).where(ResearchJobEvent.job_id == job_id).order_by(ResearchJobEvent.occurred_at)
        )
    ).all()
    return ResearchJobDetail(
        **_job(row).model_dump(),
        events=[
            ResearchJobEventResponse(
                from_status=item.from_status,
                to_status=item.to_status,
                actor=item.actor,
                detail=item.detail,
                occurred_at=item.occurred_at,
            )
            for item in events
        ],
    )


@router.post("/jobs/{job_id}/cancel", response_model=ResearchJobResponse)
async def cancel_job(
    job_id: UUID, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ResearchJobResponse:
    try:
        row = await research_jobs.request_cancel(session, job_id, actor=f"user:{user.id}")
    except research_jobs.JobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found") from exc
    await session.commit()
    await session.refresh(row)
    return _job(row)


@router.get("/worker", response_model=ResearchWorkerStatus)
async def worker_status(_: CurrentUser, session: DbSession, settings: AppSettings) -> ResearchWorkerStatus:
    now = datetime.now(UTC)
    gate = research_jobs.research_allowed(now, TradingCalendar.from_settings(settings))
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        raw = await redis.get(WORKER_HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001 - a status read must not fail the page
        raw = None
    finally:
        await redis.aclose()
    seen = parse_heartbeat(raw, now)
    counts = {
        name: len((await session.scalars(select(ResearchJob.id).where(ResearchJob.status == name))).all())
        for name in (research_jobs.QUEUED, research_jobs.RUNNING)
    }
    return ResearchWorkerStatus(
        status=seen.status,
        detail=seen.detail,
        last_seen=seen.last_seen,
        research_window_open=gate.open,
        window_detail=gate.reason,
        queued=counts[research_jobs.QUEUED],
        running=counts[research_jobs.RUNNING],
    )
