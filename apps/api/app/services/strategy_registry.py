"""Versioned, deterministic strategy definitions and persisted configurations."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting
from app.services.extra_strategies import (
    EMA_MOMENTUM_VERSION,
    VWAP_PULLBACK_VERSION,
    evaluate_ema_momentum,
    evaluate_vwap_pullback,
)
from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, STRATEGY_VERSION, StrategyDecision, evaluate_orb_retest

STRATEGIES_KEY = "paper_strategies"


@dataclass(frozen=True)
class StrategyMetadata:
    identifier: str
    name: str
    implementation_version: str
    prerequisites: tuple[str, ...]


class StrategyDefinition(Protocol):
    metadata: StrategyMetadata

    def evaluate(
        self,
        candle: CompletedCandle,
        indicators: dict,
        benchmark: dict,
        controls: dict,
        configuration: "StrategyConfiguration",
        prior_state: str,
    ) -> StrategyDecision: ...


class StrategyConfiguration(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=3, max_length=80)
    enabled: bool = True
    strategy_type: str = STRATEGY_VERSION
    version: int = Field(default=1, ge=1)
    universe: list[str] = Field(default_factory=list, max_length=200)
    allowed_sides: list[str] = Field(default_factory=lambda: ["LONG", "SHORT"])
    allowed_sessions: list[str] = Field(default_factory=lambda: ["REGULAR"])
    max_trades_per_day: int = Field(default=2, ge=1, le=20)
    max_trades_per_side: int | None = Field(default=None, ge=1, le=20)
    cooldown_minutes: int = Field(default=0, ge=0, le=240)
    risk_per_trade_percent: float | None = Field(default=None, gt=0, le=5)
    # Keep this aligned with settings.DEFAULT_TRADING_CONTROLS["minimum_score"]; a strategy
    # may still override it upward/downward through effective_controls().
    minimum_score: int = Field(default=80, ge=0, le=100)
    minimum_rr: float = Field(default=1.5, ge=1, le=10)
    volume_multiplier: float = Field(default=1.3, ge=0.5, le=10)
    retest_tolerance_percent: float = Field(default=0.15, ge=0.05, le=1)
    minimum_ema_spread_percent: float = Field(default=0.05, ge=0, le=5)

    @field_validator("universe")
    @classmethod
    def normalize_universe(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("allowed_sides")
    @classmethod
    def validate_sides(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))
        unsupported = set(normalized) - {"LONG", "SHORT"}
        if unsupported:
            raise ValueError(f"Unsupported sides: {', '.join(sorted(unsupported))}")
        return normalized

    @field_validator("allowed_sessions")
    @classmethod
    def validate_sessions(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))
        unsupported = set(normalized) - {"REGULAR"}
        if unsupported:
            raise ValueError("Only REGULAR market session is supported")
        return normalized

    def effective_controls(self, base: dict) -> dict:
        values = {
            **base,
            "minimum_score": self.minimum_score,
            "minimum_rr": self.minimum_rr,
            "volume_multiplier": self.volume_multiplier,
            "retest_tolerance_percent": self.retest_tolerance_percent,
            "minimum_ema_spread_percent": self.minimum_ema_spread_percent,
        }
        if self.risk_per_trade_percent is not None:
            values["risk_per_trade_percent"] = self.risk_per_trade_percent
        return values

    def snapshot(self, base_controls: dict) -> dict:
        return {"configuration": self.model_dump(), "effective_controls": self.effective_controls(base_controls)}

    def allows_instrument(self, instrument_token: str) -> bool:
        return not self.universe or instrument_token in self.universe

    def allows_regular_session(self) -> bool:
        return "REGULAR" in self.allowed_sessions


class OrbRetestDefinition:
    metadata = StrategyMetadata(
        identifier=STRATEGY_VERSION,
        name="Opening Range Breakout Retest",
        implementation_version=STRATEGY_VERSION,
        prerequisites=("completed candle", "opening range", "VWAP", "EMA", "volume", "market regime"),
    )

    def evaluate(self, candle, indicators, benchmark, controls, configuration, prior_state) -> StrategyDecision:
        return _direction_gated(
            evaluate_orb_retest(candle, indicators, benchmark, controls, prior_state), configuration
        )


class VwapPullbackDefinition:
    metadata = StrategyMetadata(
        identifier=VWAP_PULLBACK_VERSION,
        name="VWAP Pullback",
        implementation_version=VWAP_PULLBACK_VERSION,
        prerequisites=("completed candle", "VWAP", "EMA", "ATR", "volume", "market regime"),
    )

    def evaluate(self, candle, indicators, benchmark, controls, configuration, prior_state) -> StrategyDecision:
        return _direction_gated(
            evaluate_vwap_pullback(candle, indicators, benchmark, controls, prior_state), configuration
        )


class EmaMomentumDefinition:
    metadata = StrategyMetadata(
        identifier=EMA_MOMENTUM_VERSION,
        name="EMA Momentum",
        implementation_version=EMA_MOMENTUM_VERSION,
        prerequisites=("completed candle", "opening range", "VWAP", "EMA", "ATR", "volume"),
    )

    def evaluate(self, candle, indicators, benchmark, controls, configuration, prior_state) -> StrategyDecision:
        return _direction_gated(
            evaluate_ema_momentum(candle, indicators, benchmark, controls, prior_state), configuration
        )


def _direction_gated(decision: StrategyDecision, configuration: "StrategyConfiguration") -> StrategyDecision:
    if decision.side and decision.side not in configuration.allowed_sides:
        return StrategyDecision(next_state=AWAITING, reason=f"{decision.side.title()} signals are disabled")
    return decision


DEFAULT_STRATEGIES = [StrategyConfiguration(id="orb-retest-default", name="ORB Retest — Default").model_dump()]


class StrategyRegistry:
    """Maps persisted strategy types to deterministic implementations."""

    _definitions: dict[str, StrategyDefinition] = {
        STRATEGY_VERSION: OrbRetestDefinition(),
        VWAP_PULLBACK_VERSION: VwapPullbackDefinition(),
        EMA_MOMENTUM_VERSION: EmaMomentumDefinition(),
    }

    @classmethod
    def definition(cls, strategy_type: str) -> StrategyDefinition:
        try:
            return cls._definitions[strategy_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported strategy type: {strategy_type}") from exc

    @classmethod
    def metadata(cls) -> list[StrategyMetadata]:
        return [definition.metadata for definition in cls._definitions.values()]

    @classmethod
    async def enabled(cls, session: AsyncSession) -> list[StrategyConfiguration]:
        setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
        strategies = [
            StrategyConfiguration.model_validate(item) for item in (setting.value if setting else DEFAULT_STRATEGIES)
        ]
        return [item for item in strategies if item.enabled and item.strategy_type in cls._definitions]

    @classmethod
    def evaluate(cls, configuration, candle, indicators, benchmark, controls, prior_state) -> StrategyDecision:
        if not configuration.allows_instrument(candle.instrument_token):
            return StrategyDecision(next_state=AWAITING, reason="Instrument is outside this strategy universe")
        if not configuration.allows_regular_session():
            return StrategyDecision(next_state=AWAITING, reason="Regular-session signals are disabled")
        return cls.definition(configuration.strategy_type).evaluate(
            candle, indicators, benchmark, controls, configuration, prior_state
        )
