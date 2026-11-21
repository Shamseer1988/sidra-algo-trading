"""The swing desk's paper ledger for the Swing screens, and which strategies it runs.

The ledger views write nothing, and nothing here places, plans or cancels a
trade: the swing worker owns the ledger. The one write is a strategy's mode,
Off or Paper, chosen in the Strategies screen and recorded like any setting. Prices for open positions are the last stored
daily close, said as such -- the swing desk is not on the live tick feed.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, MarketCandle, SwingPaperPosition, User, UserRole
from app.services.desks import SWING_DESK, desk_enabled
from app.services.settings_history import record_revision, summarise
from app.services.swing_data import DAILY_TIMEFRAME_SECONDS
from app.services.swing_paper import (
    CANCELLED,
    CLOSED,
    OPEN,
    PAPER_STATUS,
    PLANNED,
    SWING_EVIDENCE,
    SWING_MODES,
    SWING_MODES_KEY,
    SWING_NAMES,
    last_processed,
    paper_strategies,
    swing_modes,
)
from app.services.swing_settings import swing_rules

router = APIRouter(prefix="/swing", tags=["Swing desk (paper)"])


class SwingPositionView(BaseModel):
    id: str
    symbol: str
    sector: str | None
    strategy: str
    status: str
    plan_date: date
    entry_trigger: float
    quantity: int
    initial_stop: float
    stop: float
    target: float | None
    risk: float
    entry_price: float | None
    entry_date: date | None
    last_close: float | None
    last_close_date: date | None
    unrealised: float | None
    r_now: float | None
    holding_days: int
    max_holding_days: int
    partial_done: bool
    next_action: str | None
    exit_date: date | None
    exit_reason: str | None
    net_pnl: float | None
    charges: float | None
    reasons: dict


class SwingSummary(BaseModel):
    enabled: bool
    paper_only: bool = True
    strategies: list[str]
    status_note: str = PAPER_STATUS
    last_processed: date | None
    capital: float
    risk_per_trade: float
    heat_cap: float
    max_open_positions: int
    invested: float
    open_risk: float
    unrealised: float
    realised: float
    closed_trades: int
    open_positions: list[SwingPositionView]
    planned: list[SwingPositionView]


def _f(value) -> float | None:  # noqa: ANN001
    return float(value) if value is not None else None


async def _last_closes(session, tokens: set[str]) -> dict[str, tuple[Decimal, date]]:  # noqa: ANN001
    closes: dict[str, tuple[Decimal, date]] = {}
    for token in tokens:
        row = await session.scalar(
            select(MarketCandle)
            .where(MarketCandle.instrument_token == token, MarketCandle.timeframe_seconds == DAILY_TIMEFRAME_SECONDS)
            .order_by(MarketCandle.session_date.desc())
            .limit(1)
        )
        if row is not None:
            closes[token] = (Decimal(str(row.close)), row.session_date)
    return closes


def view(row: SwingPaperPosition, last: tuple[Decimal, date] | None = None) -> SwingPositionView:
    state = (row.snapshot or {}).get("state", {})
    setup = (row.snapshot or {}).get("setup", {})
    remaining = int(state.get("remaining", row.quantity))
    unrealised = r_now = None
    if row.status == OPEN and last is not None and row.entry_price is not None:
        unrealised = float((last[0] - Decimal(str(row.entry_price))) * remaining)
        risk_per_share = Decimal(str(row.entry_price)) - Decimal(str(row.initial_stop))
        if risk_per_share > 0:
            r_now = float((last[0] - Decimal(str(row.entry_price))) / risk_per_share)
    next_action = None
    if row.status == OPEN:
        pending = state.get("exit_next_open")
        next_action = f"Sell at the next open ({pending})" if pending else "Hold"
    elif row.status == PLANNED:
        next_action = f"Buy-stop at {row.entry_trigger} next session"
    return SwingPositionView(
        id=str(row.id),
        symbol=row.symbol,
        sector=row.sector,
        strategy=row.strategy_version,
        status=row.status,
        plan_date=row.plan_date,
        entry_trigger=float(row.entry_trigger),
        quantity=int(row.quantity),
        initial_stop=float(row.initial_stop),
        stop=float(row.stop_price),
        target=_f(row.target_price),
        risk=float(row.risk_amount),
        entry_price=_f(row.entry_price),
        entry_date=row.entry_date,
        last_close=float(last[0]) if last else None,
        last_close_date=last[1] if last else None,
        unrealised=unrealised,
        r_now=r_now,
        holding_days=int(state.get("holding_days", 0)),
        max_holding_days=int(row.max_holding_days),
        partial_done=bool(state.get("partial_done", False)),
        next_action=next_action,
        exit_date=row.exit_date,
        exit_reason=row.exit_reason,
        net_pnl=_f(row.net_pnl),
        charges=_f(row.charges),
        reasons=setup.get("reasons", {}),
    )


@router.get("/summary", response_model=SwingSummary)
async def summary(_: CurrentUser, session: DbSession) -> SwingSummary:
    controls = await session.get(ApplicationSetting, "trading_controls")
    running = await paper_strategies(session)
    rules = await swing_rules(session, running)
    rows = list(
        (await session.scalars(select(SwingPaperPosition).where(SwingPaperPosition.status.in_((OPEN, PLANNED))))).all()
    )
    open_rows = [row for row in rows if row.status == OPEN]
    closes = await _last_closes(session, {row.instrument_token for row in open_rows})
    open_views = [view(row, closes.get(row.instrument_token)) for row in open_rows]
    closed = list((await session.scalars(select(SwingPaperPosition).where(SwingPaperPosition.status == CLOSED))).all())
    return SwingSummary(
        enabled=desk_enabled(controls.value if controls and isinstance(controls.value, dict) else {}, SWING_DESK),
        strategies=list(running),
        last_processed=await last_processed(session),
        capital=float(rules.capital),
        risk_per_trade=float(rules.risk_per_trade),
        heat_cap=float(rules.heat_cap),
        max_open_positions=rules.max_open_positions,
        invested=sum(
            float(
                Decimal(str(row.entry_price))
                * int((row.snapshot or {}).get("state", {}).get("remaining", row.quantity))
            )
            for row in open_rows
        ),
        open_risk=sum(
            max(
                0.0,
                float(
                    (Decimal(str(row.entry_price)) - Decimal(str(row.stop_price)))
                    * int((row.snapshot or {}).get("state", {}).get("remaining", row.quantity))
                ),
            )
            for row in open_rows
        ),
        unrealised=sum(item.unrealised or 0.0 for item in open_views),
        realised=sum(float(row.net_pnl or 0) for row in closed),
        closed_trades=len(closed),
        open_positions=open_views,
        planned=[view(row) for row in rows if row.status == PLANNED],
    )


@router.get("/history", response_model=list[SwingPositionView])
async def history(
    _: CurrentUser, session: DbSession, limit: int = Query(default=100, ge=1, le=500)
) -> list[SwingPositionView]:
    rows = (
        await session.scalars(
            select(SwingPaperPosition)
            .where(SwingPaperPosition.status.in_((CLOSED, CANCELLED)))
            .order_by(SwingPaperPosition.updated_at.desc())
            .limit(limit)
        )
    ).all()
    return [view(row) for row in rows]


class SwingStrategyView(BaseModel):
    id: str
    name: str
    mode: str
    live_available: bool = False
    live_blocked_reason: str = "Swing live trading is not available yet: the swing desk has no live risk limits."
    evidence: str


class SwingModeRequest(BaseModel):
    mode: str
    reason: str | None = Field(default=None, max_length=300)


@router.get("/strategies", response_model=list[SwingStrategyView])
async def swing_strategies(_: CurrentUser, session: DbSession) -> list[SwingStrategyView]:
    modes = await swing_modes(session)
    return [
        SwingStrategyView(id=name, name=SWING_NAMES.get(name, name), mode=mode, evidence=SWING_EVIDENCE.get(name, ""))
        for name, mode in modes.items()
    ]


@router.put("/strategies/{strategy_id}/mode", response_model=list[SwingStrategyView])
async def set_swing_strategy_mode(
    strategy_id: str,
    request: SwingModeRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> list[SwingStrategyView]:
    """Switch one swing strategy Off or to Paper. Live is refused, with the reason."""
    mode = (request.mode or "").strip().upper()
    if mode == "LIVE":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Swing live trading is not available yet: the swing desk has no live risk limits.",
        )
    if mode not in SWING_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Mode must be one of {', '.join(SWING_MODES)}.")
    before = await swing_modes(session)
    if strategy_id not in before:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No swing strategy with that id.")
    after = {**before, strategy_id: mode}
    row = await session.get(ApplicationSetting, SWING_MODES_KEY)
    if row is None:
        session.add(ApplicationSetting(key=SWING_MODES_KEY, value=after, updated_by_user_id=user.id))
    else:
        row.value, row.updated_by_user_id = after, user.id
    await record_revision(session, SWING_MODES_KEY, after, summarise(before, after), changed_by_user_id=user.id)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="settings.swing_strategy_mode_changed",
            metadata_json={
                "strategy_id": strategy_id,
                "from": before[strategy_id],
                "to": mode,
                "reason": request.reason,
            },
        )
    )
    await session.commit()
    return await swing_strategies(None, session)
