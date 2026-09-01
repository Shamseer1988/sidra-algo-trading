from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, User, UserRole

router = APIRouter(prefix="/settings", tags=["Settings"])
TRADING_KEY = "trading_controls"
STRATEGIES_KEY = "paper_strategies"
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


class PaperStrategy(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=3, max_length=80)
    enabled: bool = True
    strategy_type: str = "orb-retest-v1"
    minimum_score: int = Field(default=90, ge=0, le=100)
    minimum_rr: float = Field(default=1.5, ge=1, le=10)
    volume_multiplier: float = Field(default=1.3, ge=0.5, le=10)
    retest_tolerance_percent: float = Field(default=0.15, ge=0.05, le=1)
    minimum_ema_spread_percent: float = Field(default=0.05, ge=0, le=5)


DEFAULT_STRATEGIES = [PaperStrategy(name="ORB Retest — Default").model_dump()]


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


@router.get("/strategies", response_model=list[PaperStrategy])
async def get_strategies(_: CurrentUser, session: DbSession) -> list[PaperStrategy]:
    setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    return [PaperStrategy.model_validate(item) for item in (setting.value if setting else DEFAULT_STRATEGIES)]


@router.put("/strategies", response_model=list[PaperStrategy])
async def update_strategies(
    strategies: list[PaperStrategy], session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> list[PaperStrategy]:
    if not strategies or len({item.id for item in strategies}) != len(strategies):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keep one or more uniquely identified strategies"
        )
    setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    value = [item.model_dump() for item in strategies]
    if setting is None:
        session.add(ApplicationSetting(key=STRATEGIES_KEY, value=value, updated_by_user_id=user.id))
    else:
        setting.value, setting.updated_by_user_id = value, user.id
    session.add(
        AuditLog(user_id=user.id, event_type="settings.strategies_updated", metadata_json={"count": len(strategies)})
    )
    await session.commit()
    return strategies
