from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, ScannerEvaluation, User, UserRole
from app.services.risk_profile import RISK_PRESETS, effective_limits
from app.services.strategy_registry import (
    DEFAULT_STRATEGIES,
    STRATEGIES_KEY,
    StrategyConfiguration,
    StrategyRegistry,
)

router = APIRouter(prefix="/settings", tags=["Settings"])
TRADING_KEY = "trading_controls"

# Who authorises a live submission. DISABLED means no live path is offered at all.
EXECUTION_APPROVAL_MODES = frozenset({"DISABLED", "TELEGRAM_APPROVAL", "AUTOMATIC"})

# Which broker a live order would reach. NONE is the default and means no
# broker is selected, which is a refusal rather than a fallback: a system that
# picked one for you is a system that could pick the wrong one.
LIVE_BROKERS = frozenset({"NONE", "UPSTOX", "FIRSTOCK"})
# The CAUTIOUS PAPER START profile, and the reason each number is what it is.
#
# The previous defaults contradicted themselves: 1% of 10,000 is 100 of planned
# risk per trade against a 2% daily budget of 200, so the third and fourth
# trades were refused however high maximum_daily_trades was set. An operator
# reading "maximum 4 trades" got 2 and no explanation. The daily budget is now
# 4 x the per-trade risk, so the two controls say the same thing.
#
# maximum_open_exposure_percent is a percent of capital that is then multiplied
# by the leverage multiplier: 100 with 5x leverage is 50,000 of exposure on
# 10,000 of cash. That is the ceiling Shamseer specified, and it is exposure,
# never cash.
DEFAULT_TRADING_CONTROLS = {
    "account_capital": 10000.0,
    "risk_per_trade_percent": 1.0,
    "maximum_daily_risk_percent": 4.0,
    "maximum_open_positions": 1,
    "maximum_open_exposure_percent": 100.0,
    "maximum_daily_trades": 4,
    "minimum_score": 80,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "minimum_ema_spread_percent": 0.05,
    "stop_atr_multiple": 1.1,
    "min_stop_distance_percent": 0.35,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
    "intraday_leverage_enabled": True,
    "intraday_leverage_multiplier": 5.0,
    "execution_approval_mode": "DISABLED",
    "live_broker": "NONE",
    "daily_profit_target": 2000.0,
    "daily_loss_limit": 400.0,
}


class TradingControls(BaseModel):
    account_capital: float = Field(gt=0, le=100_000_000)
    risk_per_trade_percent: float = Field(gt=0, le=5)
    maximum_daily_risk_percent: float = Field(gt=0, le=10)
    maximum_open_positions: int = Field(default=3, ge=1, le=20)
    maximum_open_exposure_percent: float = Field(default=100, gt=0, le=1_000)
    # Account-wide filled entries per trading day, across every strategy and
    # both brokers. Counted from fills rather than signals: a signal whose entry
    # never filled did not use up a trade. See services/trade_counter.py.
    maximum_daily_trades: int = Field(ge=1, le=20)
    minimum_score: int = Field(ge=0, le=100)
    minimum_rr: float = Field(ge=1, le=10)
    volume_multiplier: float = Field(ge=0.5, le=10)
    retest_tolerance_percent: float = Field(ge=0.05, le=1)
    minimum_ema_spread_percent: float = Field(default=0.05, ge=0, le=5)
    # Volatility-aware stop floors. 0 disables that floor; the widest of the structural,
    # ATR, and percent distances is used as the stop.
    stop_atr_multiple: float = Field(default=1.1, ge=0, le=10)
    min_stop_distance_percent: float = Field(default=0.35, ge=0, le=5)
    trade_start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    trade_cutoff_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    intraday_leverage_enabled: bool = Field(default=True)
    intraday_leverage_multiplier: float = Field(default=5.0, ge=1.0, le=10.0)
    # How a live order reaches the broker, once a live path exists at all.
    # DISABLED is the default and the only value that is safe by construction:
    # the other two describe who authorises submission, not whether submission is
    # permitted, which remains governed by the live readiness gates.
    execution_approval_mode: str = Field(default="DISABLED")
    # The broker a live order would be sent to. Separate from the approval
    # mode because they answer different questions — who authorises a
    # submission, and where it goes — and a system where one implies the
    # other is one where changing your mind about a broker silently changes
    # who has to approve.
    live_broker: str = Field(default="NONE")

    # Realised-plus-open session P&L at which the day stops, in rupees. Zero
    # disables the limit. Deliberately rupees rather than a percentage: a daily
    # stop is an amount somebody is willing to lose today, not a ratio that
    # should quietly rescale when account_capital is edited.
    #
    # These are not maximum_daily_risk_percent, which caps how much *risk* may be
    # allocated in a session regardless of how it turns out. A day that allocates
    # its whole risk budget and wins every trade has hit that limit while making
    # money. These two are the other thing: stop when the money says stop.
    daily_profit_target: float = Field(default=0.0, ge=0, le=10_000_000)
    daily_loss_limit: float = Field(default=0.0, ge=0, le=10_000_000)

    @model_validator(mode="before")
    @classmethod
    def accept_the_old_name(cls, values: object) -> object:
        """Read a stored ``maximum_signals`` as ``maximum_daily_trades``.

        The control was renamed because it no longer counts signals, and an
        operator reading "maximum signals" would reasonably expect it to. Rows
        written before the rename keep working without a migration; a row that
        somehow carries both keeps the new one, because that is the one the UI
        will have just written.
        """
        if isinstance(values, dict) and "maximum_daily_trades" not in values and "maximum_signals" in values:
            values = {**values, "maximum_daily_trades": values["maximum_signals"]}
        return values

    @field_validator("execution_approval_mode")
    @classmethod
    def validate_approval_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in EXECUTION_APPROVAL_MODES:
            raise ValueError(f"execution_approval_mode must be one of {sorted(EXECUTION_APPROVAL_MODES)}")
        return normalized

    @field_validator("live_broker")
    @classmethod
    def validate_live_broker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in LIVE_BROKERS:
            raise ValueError(f"live_broker must be one of {sorted(LIVE_BROKERS)}")
        return normalized


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


class EffectiveLimitsResponse(BaseModel):
    """What the saved controls actually permit, once read together.

    Separate from the controls themselves because these are derived, not set. A
    UI that let an operator edit "effective trade ceiling" would be letting them
    edit a conclusion.
    """

    capital: str
    planned_risk_per_trade: str
    daily_risk_budget: str
    trades_the_budget_allows: int
    configured_trade_ceiling: int
    effective_trade_ceiling: int
    binding_control: str
    maximum_open_positions: int
    exposure_ceiling: str
    leverage_multiplier: str
    daily_loss_limit: str
    daily_loss_percent: str | None
    daily_profit_target: str
    daily_profit_percent: str | None
    warnings: list[str]


class RiskPresetResponse(BaseModel):
    key: str
    label: str
    description: str
    controls: dict
    effective: EffectiveLimitsResponse


@router.get("/trading/effective", response_model=EffectiveLimitsResponse)
async def trading_effective_limits(_: CurrentUser, session: DbSession) -> EffectiveLimitsResponse:
    """The numbers to show beside the inputs, so a contradiction is visible.

    Not a validation endpoint: nothing here refuses a combination. A deliberately
    tight daily budget under a generous trade ceiling is a reasonable thing to
    want, and no validator can tell it apart from a mistake — so it is reported
    rather than rejected.
    """
    return EffectiveLimitsResponse(**effective_limits(await _get_controls(session)).snapshot())


@router.get("/trading/presets", response_model=list[RiskPresetResponse])
async def trading_presets(_: CurrentUser) -> list[RiskPresetResponse]:
    return [
        RiskPresetResponse(
            key=key,
            label=preset["label"],
            description=preset["description"],
            controls=preset["controls"],
            effective=EffectiveLimitsResponse(
                **effective_limits({**DEFAULT_TRADING_CONTROLS, **preset["controls"]}).snapshot()
            ),
        )
        for key, preset in RISK_PRESETS.items()
    ]


def _risk_increases(before: dict, after: dict) -> list[str]:
    """Which controls were loosened, for the confirmation and the audit record.

    Only one direction is reported. Tightening a limit needs no ceremony;
    raising one is the change somebody may want to explain later.
    """
    loosened = []
    for key in (
        "risk_per_trade_percent",
        "maximum_daily_risk_percent",
        "maximum_daily_trades",
        "maximum_open_positions",
        "maximum_open_exposure_percent",
        "daily_loss_limit",
        "intraday_leverage_multiplier",
    ):
        old_value, new_value = before.get(key), after.get(key)
        if isinstance(old_value, int | float) and isinstance(new_value, int | float) and new_value > old_value:
            loosened.append(f"{key}: {old_value} -> {new_value}")
    return loosened


@router.put("/trading", response_model=TradingControls)
async def update_trading_controls(
    controls: TradingControls,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> TradingControls:
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    previous = dict(setting.value) if setting else dict(DEFAULT_TRADING_CONTROLS)
    payload = controls.model_dump()
    loosened = _risk_increases(previous, payload)

    if setting is None:
        setting = ApplicationSetting(key=TRADING_KEY, value=payload, updated_by_user_id=user.id)
        session.add(setting)
    else:
        setting.value = payload
        setting.updated_by_user_id = user.id
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="settings.trading_updated",
            metadata_json={
                "keys": sorted(payload),
                # Recorded whether or not the UI asked for confirmation: the
                # audit record is what somebody reads afterwards, and it should
                # not depend on a client having behaved.
                "risk_increased": loosened,
                "effective": effective_limits(controls).snapshot(),
            },
        )
    )
    await session.commit()
    return controls


class StrategyDefinitionResponse(BaseModel):
    identifier: str
    name: str
    prerequisites: list[str]


@router.get("/strategies/definitions", response_model=list[StrategyDefinitionResponse])
async def strategy_definitions(_: CurrentUser) -> list[StrategyDefinitionResponse]:
    return [
        StrategyDefinitionResponse(identifier=item.identifier, name=item.name, prerequisites=list(item.prerequisites))
        for item in StrategyRegistry.metadata()
    ]


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
