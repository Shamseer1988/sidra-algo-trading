"""Persisted strategy registry for deterministic paper-scanner execution."""

from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting

STRATEGIES_KEY = "paper_strategies"


class StrategyConfiguration(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=3, max_length=80)
    enabled: bool = True
    strategy_type: str = "orb-retest-v1"
    version: int = Field(default=1, ge=1)
    minimum_score: int = Field(default=90, ge=0, le=100)
    minimum_rr: float = Field(default=1.5, ge=1, le=10)
    volume_multiplier: float = Field(default=1.3, ge=0.5, le=10)
    retest_tolerance_percent: float = Field(default=0.15, ge=0.05, le=1)
    minimum_ema_spread_percent: float = Field(default=0.05, ge=0, le=5)

    def effective_controls(self, base: dict) -> dict:
        """Create the immutable settings snapshot consumed by one evaluation."""
        return {
            **base,
            "minimum_score": self.minimum_score,
            "minimum_rr": self.minimum_rr,
            "volume_multiplier": self.volume_multiplier,
            "retest_tolerance_percent": self.retest_tolerance_percent,
            "minimum_ema_spread_percent": self.minimum_ema_spread_percent,
        }


DEFAULT_STRATEGIES = [StrategyConfiguration(name="ORB Retest — Default").model_dump()]


class StrategyRegistry:
    """Loads only enabled, supported, versioned strategy configurations."""

    @staticmethod
    async def enabled(session: AsyncSession) -> list[StrategyConfiguration]:
        setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
        values = setting.value if setting else DEFAULT_STRATEGIES
        strategies = [StrategyConfiguration.model_validate(item) for item in values]
        return [item for item in strategies if item.enabled and item.strategy_type == "orb-retest-v1"]
