from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, ScannerEvaluation, User, UserRole
from app.services.strategy_registry import (
    DEFAULT_STRATEGIES,
    STRATEGIES_KEY,
    StrategyConfiguration,
    StrategyRegistry,
)

router = APIRouter(prefix="/settings", tags=["Settings"])
TRADING_KEY = "trading_controls"
DEFAULT_TRADING_CONTROLS = {
    "account_capital": 100000.0,
    "risk_per_trade_percent": 0.5,
    "maximum_daily_risk_percent": 1.0,
    "maximum_signals": 2,
    "minimum_score": 90,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "minimum_ema_spread_percent": 0.05,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
}


class TradingControls(BaseModel):
    account_capital: float = Field(gt=0, le=100_000_000)
    risk_per_trade_percent: float = Field(gt=0, le=5)
    maximum_daily_risk_percent: float = Field(gt=0, le=10)
    maximum_signals: int = Field(ge=1, le=20)
    minimum_score: int = Field(ge=0, le=100)
    minimum_rr: float = Field(ge=1, le=10)
    volume_multiplier: float = Field(ge=0.5, le=10)
    retest_tolerance_percent: float = Field(ge=0.05, le=1)
    minimum_ema_spread_percent: float = Field(default=0.05, ge=0, le=5)
    trade_start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    trade_cutoff_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class StrategyMetric(BaseModel):
    strategy_id: str
    strategy_name: str
    strategy_version: int
    evaluations: int
    accepted: int
    rejected: int
    watching: int
    acceptance_rate: float


async def _get_controls(session: DbSession) -> TradingControls:
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    return TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)


@router.get("/trading", response_model=TradingControls)
async def get_trading_controls(_: CurrentUser, session: DbSession) -> TradingControls:
    return await _get_controls(session)


@router.put("/trading", response_model=TradingControls)
async def update_trading_controls(
    controls: TradingControls,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> TradingControls:
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    if setting is None:
        setting = ApplicationSetting(key=TRADING_KEY, value=controls.model_dump(), updated_by_user_id=user.id)
        session.add(setting)
    else:
        setting.value = controls.model_dump()
        setting.updated_by_user_id = user.id
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="settings.trading_updated",
            metadata_json={"keys": sorted(controls.model_dump())},
        )
    )
    await session.commit()
    return controls


@router.get("/strategies", response_model=list[StrategyConfiguration])
async def get_strategies(_: CurrentUser, session: DbSession) -> list[StrategyConfiguration]:
    setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    return [StrategyConfiguration.model_validate(item) for item in (setting.value if setting else DEFAULT_STRATEGIES)]


@router.put("/strategies", response_model=list[StrategyConfiguration])
async def update_strategies(
    strategies: list[StrategyConfiguration], session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> list[StrategyConfiguration]:
    if not strategies or len({item.id for item in strategies}) != len(strategies):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keep one or more uniquely identified strategies"
        )
    supported = {definition.identifier for definition in StrategyRegistry.metadata()}
    unsupported = sorted({item.strategy_type for item in strategies} - supported)
    if unsupported:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported strategy type: {', '.join(unsupported)}",
        )
    setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    previous = {
        item.id: item
        for item in [
            StrategyConfiguration.model_validate(value) for value in (setting.value if setting else DEFAULT_STRATEGIES)
        ]
    }
    normalized: list[StrategyConfiguration] = []
    for item in strategies:
        old = previous.get(item.id)
        new_payload = item.model_dump(exclude={"version"})
        old_payload = old.model_dump(exclude={"version"}) if old else None
        version = old.version + 1 if old and new_payload != old_payload else old.version if old else 1
        normalized.append(item.model_copy(update={"version": version}))
    value = [item.model_dump() for item in normalized]
    if setting is None:
        session.add(ApplicationSetting(key=STRATEGIES_KEY, value=value, updated_by_user_id=user.id))
    else:
        setting.value, setting.updated_by_user_id = value, user.id
    session.add(
        AuditLog(user_id=user.id, event_type="settings.strategies_updated", metadata_json={"count": len(strategies)})
    )
    await session.commit()
    return normalized


@router.get("/strategies/metrics", response_model=list[StrategyMetric])
async def strategy_metrics(_: CurrentUser, session: DbSession) -> list[StrategyMetric]:
    rows = list(
        (
            await session.scalars(select(ScannerEvaluation).order_by(ScannerEvaluation.created_at.desc()).limit(1000))
        ).all()
    )
    metrics: dict[tuple[str, str, int], dict[str, int]] = {}
    for row in rows:
        key = (row.strategy_id, row.strategy_name, row.strategy_version)
        values = metrics.setdefault(key, {"evaluations": 0, "accepted": 0, "rejected": 0, "watching": 0})
        values["evaluations"] += 1
        if row.status == "ACCEPTED":
            values["accepted"] += 1
        elif row.status == "REJECTED":
            values["rejected"] += 1
        else:
            values["watching"] += 1
    return [
        StrategyMetric(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            **values,
            acceptance_rate=round(values["accepted"] * 100 / values["evaluations"], 2),
        )
        for (strategy_id, strategy_name, strategy_version), values in metrics.items()
    ]
