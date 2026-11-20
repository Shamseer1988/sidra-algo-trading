"""Reports > Diagnostics: list the read-only reports, and run one. See ``app.services.report_runner``."""

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AppSettings, CurrentUser
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.services import report_runner

router = APIRouter(prefix="/diagnostics", tags=["Diagnostics"])


class ReportInfo(BaseModel):
    name: str
    title: str
    help: str
    options: list[str]
    when: str


class RunRequest(BaseModel):
    session_date: date | None = None
    from_date: date | None = None
    to_date: date | None = None
    after: str | None = Field(default=None, max_length=5)
    no_broker: bool = False


class RunView(BaseModel):
    name: str
    command: str
    exit_code: int
    output: str
    started_at: datetime
    seconds: float
    truncated: bool


@router.get("/reports", response_model=list[ReportInfo])
async def reports(_: CurrentUser) -> list[ReportInfo]:
    return [
        ReportInfo(name=r.name, title=r.title, help=r.help, options=list(r.options), when=r.when)
        for r in report_runner.REPORTS.values()
    ]


@router.post("/reports/{name}", response_model=RunView)
async def run_report(name: str, request: RunRequest, settings: AppSettings, user: CurrentUser) -> RunView:
    """Run one read-only report. It writes nothing and cannot place, change or cancel an order."""
    try:
        result = await report_runner.run(
            settings,
            name,
            day=request.session_date,
            start=request.from_date,
            end=request.to_date,
            after=request.after,
            no_broker=request.no_broker,
        )
    except report_runner.ReportError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user.id,
                event_type="diagnostics.report_run",
                metadata_json={"report": name, "command": result.command},
            )
        )
        await session.commit()
    return RunView(
        name=result.name,
        command=result.command,
        exit_code=result.exit_code,
        output=result.output,
        started_at=result.started_at,
        seconds=result.seconds,
        truncated=result.truncated,
    )
