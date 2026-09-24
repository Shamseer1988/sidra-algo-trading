from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, ScannerEvaluation, User, UserRole
from app.services.indicator_settings import (
    INDICATOR_KEY,
    IndicatorSettings,
    from_environment,
)
from app.services.indicator_settings import load as load_indicators
from app.services.risk_profile import RISK_PRESETS, effective_limits
from app.services.settings_catalog import (
    GROUP_LABELS,
    GROUP_ORDER,
    INDICATOR_SPECS,
    TRADING_CONTROL_SPECS,
)
from app.services.settings_history import last_changed_per_key, record_revision, revision_history, summarise
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


@router.put("/trading", response_model=TradingControls)
async def update_trading_controls(
    controls: TradingControls,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> TradingControls:
    return await _persist_controls(session, controls, user)


async def _persist_controls(session: DbSession, controls: TradingControls, user: User) -> TradingControls:
    """Save, version and audit one settings change. The one write path.

    Applying a preset goes through here too, so a preset is validated, recorded
    and audited exactly as a hand edit is. A second path that skipped any of
    that would be a way to change risk limits without leaving a trace.
    """
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    previous = dict(setting.value) if setting else dict(DEFAULT_TRADING_CONTROLS)
    payload = controls.model_dump()
    summary = summarise(previous, payload)

    if setting is None:
        setting = ApplicationSetting(key=TRADING_KEY, value=payload, updated_by_user_id=user.id)
        session.add(setting)
    else:
        setting.value = payload
        setting.updated_by_user_id = user.id

    await record_revision(session, TRADING_KEY, payload, summary, changed_by_user_id=user.id)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="settings.trading_updated",
            metadata_json={
                "changed_keys": summary.changed_keys,
                # Recorded whether or not the UI asked for confirmation: the
                # audit record is what somebody reads afterwards, and it should
                # not depend on a client having behaved.
                "risk_increased": summary.risk_increased,
                "effective": effective_limits(controls).snapshot(),
            },
        )
    )
    await session.commit()
    return controls


class SettingSpecResponse(BaseModel):
    """One control, described well enough for a form to be generated from it."""

    key: str
    group: str
    group_label: str
    label: str
    help: str
    unit: str
    kind: str
    choices: list[str]
    is_ceiling: bool
    effect: str
    effect_label: str
    value: object
    minimum: float | None
    maximum: float | None
    exclusive_minimum: float | None
    exclusive_maximum: float | None
    last_changed_at: str | None


class SettingsCatalogResponse(BaseModel):
    group_order: list[str]
    group_labels: dict[str, str]
    settings: list[SettingSpecResponse]
    effective: EffectiveLimitsResponse


@router.get("/trading/catalog", response_model=SettingsCatalogResponse)
async def trading_catalog(_: CurrentUser, session: DbSession) -> SettingsCatalogResponse:
    """Everything a settings form needs, so none of it is hard-coded in the UI.

    Bounds come from the schema rather than from the catalogue, so a form can
    never offer a range the server will then refuse.
    """
    controls = await _get_controls(session)
    values = controls.model_dump()
    stamps = await last_changed_per_key(session, TRADING_KEY)
    return SettingsCatalogResponse(
        group_order=list(GROUP_ORDER),
        group_labels=dict(GROUP_LABELS),
        settings=[
            SettingSpecResponse(
                **spec.describe(TradingControls.model_fields, values.get(spec.key), stamps.get(spec.key))
            )
            for spec in TRADING_CONTROL_SPECS
        ],
        effective=EffectiveLimitsResponse(**effective_limits(controls).snapshot()),
    )


class SettingRevisionResponse(BaseModel):
    created_at: datetime
    changed_keys: list[str]
    risk_increased: list[str]
    changed_by_user_id: str | None


@router.get("/trading/history", response_model=list[SettingRevisionResponse])
async def trading_history(_: CurrentUser, session: DbSession) -> list[SettingRevisionResponse]:
    return [
        SettingRevisionResponse(
            created_at=revision.created_at,
            changed_keys=list(revision.changed_keys or []),
            risk_increased=list(revision.risk_increased or []),
            changed_by_user_id=str(revision.changed_by_user_id) if revision.changed_by_user_id else None,
        )
        for revision in await revision_history(session, TRADING_KEY)
    ]


class ApplyPresetRequest(BaseModel):
    preset: str
    # The operator's acknowledgement, required when the preset loosens a limit.
    # Checked server-side because a confirmation a client can skip is not one.
    confirm_risk_increase: bool = False


@router.post("/trading/presets/{preset}", response_model=TradingControls)
async def apply_preset(
    preset: str,
    request: ApplyPresetRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> TradingControls:
    """Apply a named risk profile, through the same path as any other edit.

    A preset that loosens a limit needs the same acknowledgement a hand edit
    does. Enforced here rather than in the UI: a confirmation that lives only in
    a client is a confirmation that can be skipped by calling the API.
    """
    if preset not in RISK_PRESETS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown preset {preset!r}; expected one of {sorted(RISK_PRESETS)}",
        )
    current = await _get_controls(session)
    candidate = TradingControls.model_validate({**current.model_dump(), **RISK_PRESETS[preset]["controls"]})
    summary = summarise(current.model_dump(), candidate.model_dump())
    if summary.risk_increased and not request.confirm_risk_increase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This preset raises a risk limit: "
                + "; ".join(summary.risk_increased)
                + ". Re-send with confirm_risk_increase to apply it."
            ),
        )
    return await _persist_controls(session, candidate, user)


# --- indicator periods ----------------------------------------------------


class IndicatorCatalogResponse(BaseModel):
    settings: list[SettingSpecResponse]
    source: str


@router.get("/indicators", response_model=IndicatorSettings)
async def get_indicator_settings(_: CurrentUser, session: DbSession, settings: AppSettings) -> IndicatorSettings:
    return await load_indicators(session, settings)


@router.get("/indicators/catalog", response_model=IndicatorCatalogResponse)
async def indicator_catalog(_: CurrentUser, session: DbSession, settings: AppSettings) -> IndicatorCatalogResponse:
    """The periods, described, plus where the current values came from.

    ``source`` is worth showing: until somebody saves these through the UI the
    deployment is still running on its ``.env``, and an operator who does not
    know that will wonder why editing the file used to work and now does not.
    """
    stored = await session.get(ApplicationSetting, INDICATOR_KEY)
    values = (await load_indicators(session, settings)).model_dump()
    stamps = await last_changed_per_key(session, INDICATOR_KEY)
    return IndicatorCatalogResponse(
        settings=[
            SettingSpecResponse(
                **spec.describe(IndicatorSettings.model_fields, values.get(spec.key), stamps.get(spec.key))
            )
            for spec in INDICATOR_SPECS
        ],
        source="DATABASE" if stored is not None else "ENVIRONMENT",
    )


@router.put("/indicators", response_model=IndicatorSettings)
async def update_indicator_settings(
    indicators: IndicatorSettings,
    session: DbSession,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> IndicatorSettings:
    """Save the periods, versioned and audited like any other setting.

    The first save is the moment this deployment stops reading ``.env`` for
    these values, which the audit entry records so the change of source is not
    something somebody has to infer later.
    """
    stored = await session.get(ApplicationSetting, INDICATOR_KEY)
    previous = (
        dict(stored.value)
        if stored is not None and isinstance(stored.value, dict)
        else from_environment(settings).model_dump()
    )
    payload = indicators.model_dump()
    summary = summarise(previous, payload)

    if stored is None:
        stored = ApplicationSetting(key=INDICATOR_KEY, value=payload, updated_by_user_id=user.id)
        session.add(stored)
    else:
        stored.value = payload
        stored.updated_by_user_id = user.id

    await record_revision(session, INDICATOR_KEY, payload, summary, changed_by_user_id=user.id)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="settings.indicators_updated",
            metadata_json={
                "changed_keys": summary.changed_keys,
                "previous_source": "DATABASE" if stored.created_at else "ENVIRONMENT",
            },
        )
    )
    await session.commit()
    return indicators


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
