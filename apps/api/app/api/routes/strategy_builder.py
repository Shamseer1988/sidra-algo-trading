"""Strategies > Builder: make a new intraday strategy, measure it, save it -- never live.

A strategy here is one of the tested strategy types (the code that decides a
signal) with its rules set: sides, score, reward:risk, volume, universe, risk,
trade caps and exit rules. Nothing here writes new signal logic; that would be
untested code deciding trades.

What this screen can and cannot do, enforced here rather than by the screen:

* **Backtest a draft without saving it.** The draft is replayed over stored
  candles by the same code Backtesting uses; the run is kept as research.
* **Save** a new strategy. It is always stored switched off and on paper. It
  starts trading on paper only when someone sets it to Paper in the control
  center, and goes live only through the control center's own confirmation.
* **Edit** a strategy's rules, one strategy at a time, never its mode. A
  strategy that is live is changed only with ``confirm_live`` and a reason,
  which the screen asks for in its own "real money" step; the change is
  versioned, revisioned and audited as a live edit.
* **Delete** a strategy that is Off. Its history stays; only the configuration
  goes.
"""

from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.api.routes.backtesting import BacktestRunDetail, BacktestRunRequest, execute_backtest
from app.api.routes.settings import (
    DEFAULT_TRADING_CONTROLS,
    TRADING_KEY,
    TradingControls,
    _persist_strategies,
    _stored_strategies,
    strategy_mode,
)
from app.db.models import ApplicationSetting, User, UserRole
from app.services import watchlist_store
from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PAPER_EXECUTION_KEY, PaperExecutionControls
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry
from app.services.trading_symbols import resolve_script_names
from app.services.upstox_market_data import configured_subscriptions

router = APIRouter(prefix="/strategy-builder", tags=["Strategy builder"])
Admin = Depends(require_roles(UserRole.ADMIN))

# One paragraph per tested strategy type, in the words an operator would use.
ABOUT: dict[str, str] = {
    "orb-retest-v1": (
        "Waits for the first 15 minutes' range to break, then for price to come back and hold the broken level "
        "(the retest) with volume, VWAP and EMA agreeing."
    ),
    "vwap-pullback-v1": (
        "In a trending stock, buys a pullback to VWAP (or sells a rally to it) when the EMAs and volume confirm the trend."
    ),
    "ema-momentum-v1": "Follows a strong move when the fast EMA is clearly above (or below) the slow one and volume expands.",
    "rs-pullback-v1": (
        "Picks shares stronger (or weaker) than NIFTY and enters on a pullback that holds, in the direction of that "
        "relative strength. This is the type the live strategy uses."
    ),
    "cpr-breakout-v1": (
        "Uses the previous day's central pivot range and the first hour's range; enters when price breaks out of both."
    ),
}


class TemplateView(BaseModel):
    strategy_type: str
    name: str
    about: str
    prerequisites: list[str]
    defaults: StrategyConfiguration


class ExistingView(BaseModel):
    id: str
    name: str
    strategy_type: str
    mode: str
    version: int
    configuration: StrategyConfiguration


class BuilderCatalog(BaseModel):
    templates: list[TemplateView]
    strategies: list[ExistingView]


class Instrument(BaseModel):
    instrument_token: str
    symbol: str


class DraftBacktest(BaseModel):
    draft: StrategyConfiguration
    start_date: date
    end_date: date
    instrument_tokens: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def check_range(self) -> "DraftBacktest":
        if self.end_date < self.start_date:
            raise ValueError("The end date is before the start date.")
        if (self.end_date - self.start_date).days > 31:
            raise ValueError("A backtest covers at most 32 days at a time.")
        return self


class SaveRequest(BaseModel):
    draft: StrategyConfiguration
    reason: str | None = Field(default=None, max_length=300)
    # Required, with a reason, to change the rules of a strategy that is live.
    # The screen sets it only after its own "this trades real money" step.
    confirm_live: bool = False


def _refuse(code: int, detail: str) -> HTTPException:
    return HTTPException(code, detail)


def _check_type(draft: StrategyConfiguration) -> None:
    try:
        StrategyRegistry.definition(draft.strategy_type)
    except ValueError as exc:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _check_name(draft: StrategyConfiguration, existing: dict[str, StrategyConfiguration], own_id: str | None) -> None:
    wanted = draft.name.strip().lower()
    for item in existing.values():
        if item.id != own_id and item.name.strip().lower() == wanted:
            raise _refuse(status.HTTP_409_CONFLICT, f"There is already a strategy called '{item.name}'.")


@router.get("/catalog", response_model=BuilderCatalog)
async def catalog(session: DbSession, _: CurrentUser) -> BuilderCatalog:
    templates = [
        TemplateView(
            strategy_type=meta.identifier,
            name=meta.name,
            about=ABOUT.get(meta.identifier, ""),
            prerequisites=list(meta.prerequisites),
            defaults=StrategyConfiguration(
                id="new",
                name=f"My {meta.name}"[:80],
                strategy_type=meta.identifier,
                enabled=False,
                live_orders=False,
            ),
        )
        for meta in StrategyRegistry.metadata()
    ]
    stored = await _stored_strategies(session)
    return BuilderCatalog(
        templates=templates,
        strategies=[
            ExistingView(
                id=item.id,
                name=item.name,
                strategy_type=item.strategy_type,
                mode=strategy_mode(item),
                version=item.version,
                configuration=item,
            )
            for item in stored.values()
        ],
    )


@router.get("/instruments", response_model=list[Instrument])
async def instruments(session: DbSession, settings: AppSettings, _: CurrentUser) -> list[Instrument]:
    """The shares the scanner watches, by name, for picking a backtest's instruments."""
    settings = watchlist_store.apply(settings, await watchlist_store.load(session))
    keys = [
        key
        for key in configured_subscriptions(settings)
        if not key.startswith(("NSE_INDEX|", "BSE_INDEX|"))
        and key not in {settings.upstox_nifty_benchmark_key, settings.upstox_india_vix_key}
    ]
    names = await resolve_script_names(session, keys)
    rows = [Instrument(instrument_token=key, symbol=names.get(key, key)) for key in keys]
    return sorted(rows, key=lambda row: row.symbol)


@router.post("/backtest", response_model=BacktestRunDetail, status_code=status.HTTP_201_CREATED)
async def backtest_draft(
    request: DraftBacktest, app_settings: AppSettings, session: DbSession, user: User = Admin
) -> BacktestRunDetail:
    """Replay an unsaved draft. Nothing is saved but the research run itself."""
    _check_type(request.draft)
    # On for the replay (a backtest only replays enabled strategies), and
    # never anything but paper: a backtest places nothing either way.
    draft = request.draft.model_copy(
        update={"enabled": True, "live_orders": False, "id": request.draft.id or f"draft-{uuid4().hex[:8]}"}
    )
    row = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS).model_dump()
    row = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    execution = PaperExecutionControls.model_validate(row.value if row else DEFAULT_PAPER_EXECUTION_CONTROLS)
    run_request = BacktestRunRequest(
        start_date=request.start_date,
        end_date=request.end_date,
        instrument_tokens=request.instrument_tokens,
        strategy_ids=[draft.id],
        timeframe_seconds=60,
    )
    return await execute_backtest(run_request, app_settings, session, user, [draft], controls, execution)


@router.post("/strategies", response_model=list[StrategyConfiguration], status_code=status.HTTP_201_CREATED)
async def save_new(request: SaveRequest, session: DbSession, user: User = Admin) -> list[StrategyConfiguration]:
    """Save a new strategy: always Off and paper-only, whatever the draft says."""
    _check_type(request.draft)
    previous = await _stored_strategies(session)
    _check_name(request.draft, previous, None)
    created = request.draft.model_copy(
        update={"id": str(uuid4()), "enabled": False, "live_orders": False, "version": 1}
    )
    return await _persist_strategies(
        session,
        [*previous.values(), created],
        previous,
        user,
        "strategy_builder.created",
        {"strategy_id": created.id, "strategy_type": created.strategy_type, "reason": request.reason},
    )


@router.put("/strategies/{strategy_id}", response_model=list[StrategyConfiguration])
async def save_existing(
    strategy_id: str, request: SaveRequest, session: DbSession, user: User = Admin
) -> list[StrategyConfiguration]:
    """Change one strategy's rules. Refused while it is live; its mode is never changed here."""
    _check_type(request.draft)
    previous = await _stored_strategies(session)
    current = previous.get(strategy_id)
    if current is None:
        raise _refuse(status.HTTP_404_NOT_FOUND, "No strategy with that id.")
    is_live = strategy_mode(current) == "LIVE"
    if is_live and not (request.confirm_live and (request.reason or "").strip()):
        raise _refuse(
            status.HTTP_409_CONFLICT,
            "This strategy is live and trades real money. Confirm the change and say why it is being made.",
        )
    _check_name(request.draft, previous, strategy_id)
    updated = request.draft.model_copy(
        update={"id": strategy_id, "enabled": current.enabled, "live_orders": current.live_orders}
    )
    return await _persist_strategies(
        session,
        [updated if item.id == strategy_id else item for item in previous.values()],
        previous,
        user,
        "strategy_builder.updated",
        {"strategy_id": strategy_id, "reason": request.reason, "live": is_live},
    )


@router.delete("/strategies/{strategy_id}", response_model=list[StrategyConfiguration])
async def delete(strategy_id: str, session: DbSession, user: User = Admin) -> list[StrategyConfiguration]:
    """Remove a strategy that is Off. Signals, trades and backtests it made are kept."""
    previous = await _stored_strategies(session)
    current = previous.get(strategy_id)
    if current is None:
        raise _refuse(status.HTTP_404_NOT_FOUND, "No strategy with that id.")
    if strategy_mode(current) != "OFF":
        raise _refuse(status.HTTP_409_CONFLICT, "Only a strategy that is Off can be deleted. Switch it off first.")
    remaining = [item for item in previous.values() if item.id != strategy_id]
    if not remaining:
        raise _refuse(status.HTTP_409_CONFLICT, "The last strategy cannot be deleted.")
    return await _persist_strategies(
        session,
        remaining,
        previous,
        user,
        "strategy_builder.deleted",
        {"strategy_id": strategy_id, "name": current.name},
    )
