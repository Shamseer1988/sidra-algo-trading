"""Swing > Data and Swing > Backtest: coverage, fetching daily candles, backtests, corporate events.

Starting a job returns at once; the job runs in the background and its state is
read from ``GET /swing/jobs``. Heavy jobs are refused during market hours.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, CorporateEvent, User, UserRole
from app.services import swing_tools
from app.services.swing_strategies import STRATEGIES
from app.services.swing_universe import nifty200

router = APIRouter(prefix="/swing", tags=["Swing desk (tools)"])

# The types the corporate_events table allows. A demerger lowers the parent's price the same way a
# split does, so it is recorded as a SPLIT with the observed factor and a note saying so.
EVENT_TYPES = ("SPLIT", "BONUS", "DIVIDEND", "RESULTS")
ADJUSTING = ("SPLIT", "BONUS")


class JobsView(BaseModel):
    market_hours: bool
    jobs: dict[str, dict]


class BackfillRequest(BaseModel):
    years: int = Field(default=2, ge=1, le=5)


class BacktestRequest(BaseModel):
    years: int = Field(default=2, ge=1, le=5)
    strategies: list[str] = Field(default_factory=lambda: list(STRATEGIES))


class EventView(BaseModel):
    id: str
    symbol: str | None
    instrument_token: str
    event_type: str
    ex_date: date
    price_factor: float | None
    amount: float | None
    confirmed: bool
    source: str
    note: str | None


class EventRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    event_type: str
    ex_date: date
    price_factor: float | None = Field(default=None, gt=1, le=100)
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=255)


def _event_view(row: CorporateEvent) -> EventView:
    return EventView(
        id=str(row.id),
        symbol=row.symbol,
        instrument_token=row.instrument_token,
        event_type=row.event_type,
        ex_date=row.ex_date,
        price_factor=float(row.price_factor) if row.price_factor is not None else None,
        amount=float(row.amount) if row.amount is not None else None,
        confirmed=row.confirmed,
        source=row.source,
        note=row.note,
    )


async def _jobs_view() -> JobsView:
    return JobsView(market_hours=swing_tools.market_hours(), jobs=await swing_tools.jobs())


@router.get("/jobs", response_model=JobsView)
async def jobs(_: CurrentUser) -> JobsView:
    return await _jobs_view()


@router.get("/data/coverage")
async def data_coverage(settings: AppSettings, _: CurrentUser, years: int = 2) -> dict:
    """Read-only: what is stored, what is missing, and which drops look like an unrecorded split."""
    return await swing_tools.coverage_report(settings, years=max(1, min(years, 5)))


async def _start(kind: str, params: dict, user: User):  # noqa: ANN202
    try:
        job = await swing_tools.claim(kind, user.id, params)
    except swing_tools.SwingToolError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return job


@router.post("/data/backfill", response_model=JobsView, status_code=status.HTTP_202_ACCEPTED)
async def start_backfill(
    request: BackfillRequest,
    background: BackgroundTasks,
    settings: AppSettings,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> JobsView:
    await _start("backfill", request.model_dump(), user)
    session.add(AuditLog(user_id=user.id, event_type="swing.backfill_started", metadata_json=request.model_dump()))
    await session.commit()
    background.add_task(swing_tools.run_backfill, settings, request.years)
    return await _jobs_view()


@router.post("/backtests", response_model=JobsView, status_code=status.HTTP_202_ACCEPTED)
async def start_backtest(
    request: BacktestRequest,
    background: BackgroundTasks,
    settings: AppSettings,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> JobsView:
    unknown = sorted(set(request.strategies) - set(STRATEGIES))
    if unknown or not request.strategies:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose at least one known swing strategy.")
    await _start("backtest", request.model_dump(), user)
    session.add(AuditLog(user_id=user.id, event_type="swing.backtest_started", metadata_json=request.model_dump()))
    await session.commit()
    background.add_task(swing_tools.run_backtest_job, settings, request.years, request.strategies)
    return await _jobs_view()


@router.get("/backtests")
async def backtests(_: CurrentUser) -> list[dict]:
    return await swing_tools.backtest_runs()


@router.get("/corporate-events", response_model=list[EventView])
async def corporate_events(_: CurrentUser, session: DbSession) -> list[EventView]:
    rows = (await session.scalars(select(CorporateEvent).order_by(CorporateEvent.ex_date.desc()))).all()
    return [_event_view(row) for row in rows]


@router.post("/corporate-events", response_model=EventView, status_code=status.HTTP_201_CREATED)
async def record_event(
    request: EventRequest, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> EventView:
    """Record one event a person has checked. A split or bonus (or a demerger, recorded as a split) adjusts prices before its ex-date."""
    kind = request.event_type.strip().upper()
    if kind not in EVENT_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Type must be one of {', '.join(EVENT_TYPES)}.")
    if kind in ADJUSTING and request.price_factor is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"A {kind.lower()} needs a price factor above 1: what prices before the ex-date are divided by.",
        )
    if kind not in ADJUSTING and request.price_factor is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"A {kind.lower()} does not adjust prices; leave the factor empty."
        )
    if kind == "DIVIDEND" and request.amount is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A dividend needs the amount per share.")
    symbol = request.symbol.strip().upper()
    member = next((m for m in nifty200() if m.symbol == symbol), None)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} is not in the NIFTY 200 list the swing desk uses.")
    duplicate = await session.scalar(
        select(CorporateEvent).where(
            CorporateEvent.instrument_token == member.instrument_token,
            CorporateEvent.event_type == kind,
            CorporateEvent.ex_date == request.ex_date,
        )
    )
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That event is already recorded.")
    row = CorporateEvent(
        instrument_token=member.instrument_token,
        symbol=symbol,
        event_type=kind,
        ex_date=request.ex_date,
        price_factor=Decimal(str(request.price_factor)) if request.price_factor is not None else None,
        amount=Decimal(str(request.amount)) if request.amount is not None else None,
        confirmed=True,
        source="screen",
        note=request.note,
    )
    session.add(row)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="swing.corporate_event_recorded",
            metadata_json={
                "symbol": symbol,
                "type": kind,
                "ex_date": request.ex_date.isoformat(),
                "factor": request.price_factor,
            },
        )
    )
    await session.commit()
    return _event_view(row)


@router.delete("/corporate-events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(event_id: UUID, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))) -> None:
    row = await session.get(CorporateEvent, event_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event.")
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="swing.corporate_event_deleted",
            metadata_json={"symbol": row.symbol, "type": row.event_type, "ex_date": row.ex_date.isoformat()},
        )
    )
    await session.delete(row)
    await session.commit()
