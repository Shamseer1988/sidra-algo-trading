"""Strategies > Subscriptions: the intraday watchlist and benchmark. See ``app.services.watchlist_store``."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, User, UserRole
from app.services import watchlist_store
from app.services.trading_calendar import MARKET_TIMEZONE
from app.services.trading_symbols import resolve_script_names

router = APIRouter(prefix="/watchlist", tags=["Watchlist"])


class ShareView(BaseModel):
    instrument_key: str
    symbol: str
    name: str
    in_nifty200: bool
    known: bool
    sector: str | None


class BenchmarkOption(BaseModel):
    key: str
    name: str


class WatchlistView(BaseModel):
    source: str  # "screen" or "server file"
    benchmark: str
    benchmark_name: str
    benchmarks: list[BenchmarkOption]
    shares: list[ShareView]
    applies: str
    max_shares: int
    updated_at: datetime | None


class SearchResult(BaseModel):
    instrument_key: str
    symbol: str
    name: str
    in_nifty200: bool
    source: str
    sector: str | None


class SaveRequest(BaseModel):
    instruments: list[str] = Field(min_length=1, max_length=watchlist_store.MAX_SHARES)
    benchmark: str
    reason: str = Field(min_length=1, max_length=300)
    # Set by the screen only after its confirmation popup, which says the live
    # strategy may trade an added share with real money.
    confirm: bool = False


def _applies_now() -> str:
    now = datetime.now(MARKET_TIMEZONE)
    if now.weekday() < 5 and (now.hour, now.minute) < watchlist_store.PRE_OPEN_CUTOFF:
        return "A change saved now applies from this morning's session (before 9:00 AM)."
    return "A change saved now applies from the next trading day; today's scan keeps the list it started with."


async def _view(session, settings) -> WatchlistView:  # noqa: ANN001
    saved = await watchlist_store.load(session)
    row = await session.get(ApplicationSetting, watchlist_store.WATCHLIST_KEY)
    effective = watchlist_store.apply(settings, saved)
    keys = [k for k in watchlist_store.env_instruments(effective) if not watchlist_store.is_index(k)]
    names = await resolve_script_names(session, keys)
    benchmark = effective.upstox_nifty_benchmark_key
    return WatchlistView(
        source="screen" if saved else "server file",
        benchmark=benchmark,
        benchmark_name=watchlist_store.BENCHMARKS.get(benchmark, benchmark.split("|", 1)[-1]),
        benchmarks=[BenchmarkOption(key=k, name=v) for k, v in watchlist_store.BENCHMARKS.items()],
        shares=[ShareView(**row) for row in await watchlist_store.describe(keys, names)],
        applies=_applies_now(),
        max_shares=watchlist_store.MAX_SHARES,
        updated_at=row.updated_at if row is not None and saved else None,
    )


@router.get("", response_model=WatchlistView)
async def watchlist(session: DbSession, settings: AppSettings, _: CurrentUser) -> WatchlistView:
    return await _view(session, settings)


@router.get("/search", response_model=list[SearchResult])
async def search(_: CurrentUser, q: str = Query(min_length=2, max_length=40)) -> list[SearchResult]:
    """Shares matching a symbol or a company name, with the full name to confirm against."""
    rows = []
    for listing in await watchlist_store.search(q):
        group = watchlist_store.sector_of(listing.symbol)
        rows.append(
            SearchResult(
                instrument_key=listing.instrument_key,
                symbol=listing.symbol,
                name=listing.name,
                in_nifty200=listing.in_nifty200,
                source=listing.source,
                sector=None if group.startswith("UNMAPPED:") else group,
            )
        )
    return rows


@router.put("", response_model=WatchlistView)
async def save(
    request: SaveRequest,
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> WatchlistView:
    """Replace the watchlist and benchmark. Needs ``confirm`` and a reason."""
    if not request.confirm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Confirm the change first.")
    try:
        saved = await watchlist_store.validate(request.instruments, request.benchmark)
    except watchlist_store.WatchlistError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    before = watchlist_store.apply(settings, await watchlist_store.load(session))
    old_keys = set(watchlist_store.env_instruments(before))
    new_keys = set(saved.instruments)
    row = await session.get(ApplicationSetting, watchlist_store.WATCHLIST_KEY)
    if row is None:
        session.add(
            ApplicationSetting(key=watchlist_store.WATCHLIST_KEY, value=saved.model_dump(), updated_by_user_id=user.id)
        )
    else:
        row.value, row.updated_by_user_id = saved.model_dump(), user.id
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="watchlist.updated",
            metadata_json={
                "added": sorted(new_keys - old_keys),
                "removed": sorted(old_keys - new_keys),
                "benchmark_from": before.upstox_nifty_benchmark_key,
                "benchmark_to": saved.benchmark,
                "count": len(saved.instruments),
                "reason": request.reason,
            },
        )
    )
    await session.commit()
    return await _view(session, settings)


@router.delete("", response_model=WatchlistView)
async def use_server_file(
    session: DbSession, settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> WatchlistView:
    """Forget the saved list and benchmark; the server's ``.env`` applies again."""
    row = await session.get(ApplicationSetting, watchlist_store.WATCHLIST_KEY)
    if row is not None:
        await session.delete(row)
        session.add(AuditLog(user_id=user.id, event_type="watchlist.reset_to_env", metadata_json={}))
        await session.commit()
    return await _view(session, settings)
