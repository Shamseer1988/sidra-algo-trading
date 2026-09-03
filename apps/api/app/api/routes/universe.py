"""Read the dynamic scan universe and (for administrators) rebuild it on demand."""

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import ScanUniverseEntry, User, UserRole
from app.services.trading_symbols import resolve_script_names
from app.services.universe import refresh_universe

router = APIRouter(prefix="/universe", tags=["Universe"])


class UniverseEntryResponse(BaseModel):
    instrument_token: str
    script_name: str
    session_date: str
    rank: int
    score: float
    selected: bool
    eligible: bool
    rejection_reason: str | None
    liquidity_score: float
    volatility_score: float
    gap_score: float
    trend_score: float
    metrics: dict


class UniverseSummaryResponse(BaseModel):
    session_date: str
    enabled: bool
    universe_size: int
    total_candidates: int
    eligible: int
    selected: int
    last_built_at: str | None


def _entry(row: ScanUniverseEntry, script_name: str) -> UniverseEntryResponse:
    return UniverseEntryResponse(
        instrument_token=row.instrument_token,
        script_name=script_name,
        session_date=row.session_date.isoformat(),
        rank=row.rank,
        score=float(row.score),
        selected=row.selected,
        eligible=row.eligible,
        rejection_reason=row.rejection_reason,
        liquidity_score=float(row.liquidity_score),
        volatility_score=float(row.volatility_score),
        gap_score=float(row.gap_score),
        trend_score=float(row.trend_score),
        metrics=row.metrics or {},
    )


async def _entries(session: AsyncSession, session_date: date) -> list[ScanUniverseEntry]:
    return list(
        (
            await session.scalars(
                select(ScanUniverseEntry)
                .where(ScanUniverseEntry.session_date == session_date)
                .order_by(ScanUniverseEntry.rank.asc())
            )
        ).all()
    )


@router.get("", response_model=list[UniverseEntryResponse])
async def list_universe(
    _: CurrentUser, session: DbSession, session_date: date | None = None
) -> list[UniverseEntryResponse]:
    target = session_date or date.today()
    rows = await _entries(session, target)
    names = await resolve_script_names(session, {row.instrument_token for row in rows})
    return [_entry(row, names.get(row.instrument_token, row.instrument_token)) for row in rows]


@router.get("/summary", response_model=UniverseSummaryResponse)
async def universe_summary(
    settings: AppSettings, _: CurrentUser, session: DbSession, session_date: date | None = None
) -> UniverseSummaryResponse:
    target = session_date or date.today()
    rows = await _entries(session, target)
    built_at = max((row.created_at for row in rows), default=None)
    return UniverseSummaryResponse(
        session_date=target.isoformat(),
        enabled=settings.universe_enabled,
        universe_size=settings.universe_size,
        total_candidates=len(rows),
        eligible=sum(row.eligible for row in rows),
        selected=sum(row.selected for row in rows),
        last_built_at=built_at.isoformat() if built_at else None,
    )


@router.post("/refresh", response_model=UniverseSummaryResponse)
async def rebuild_universe(
    settings: AppSettings,
    session: DbSession,
    _: User = Depends(require_roles(UserRole.ADMIN, UserRole.TRADER)),
    session_date: date | None = None,
) -> UniverseSummaryResponse:
    target = session_date or date.today()
    await refresh_universe(settings, target)
    rows = await _entries(session, target)
    built_at = max((row.created_at for row in rows), default=None)
    return UniverseSummaryResponse(
        session_date=target.isoformat(),
        enabled=settings.universe_enabled,
        universe_size=settings.universe_size,
        total_candidates=len(rows),
        eligible=sum(row.eligible for row in rows),
        selected=sum(row.selected for row in rows),
        last_built_at=built_at.isoformat() if built_at else None,
    )
