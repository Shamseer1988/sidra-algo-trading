"""Settings > Swing trading: the paper desk's capital and limits, from the screen.

Until now these lived only as the defaults of ``SwingRules``. They are stored
in ``application_settings`` under ``swing_desk_rules`` (no migration) and read
once per run by the swing worker, the swing summary and the swing backtest, so
an evening never mixes two sets.

The swing desk is paper only: the live risk engine refuses every swing order
regardless, so nothing here can reach a broker. The defaults are exactly the
values the desk ran with before this screen existed; with nothing saved,
nothing changes.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting
from app.services.swing_backtest import SwingRules

SWING_RULES_KEY = "swing_desk_rules"


class SwingRuleSettings(BaseModel):
    """The operator's units: rupees, percent of capital, counts and sessions."""

    capital: float = Field(default=100_000, ge=10_000, le=10_000_000)
    risk_per_trade_percent: float = Field(default=0.75, gt=0, le=3)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    max_per_sector: int = Field(default=1, ge=1, le=10)
    heat_cap_percent: float = Field(default=4, gt=0, le=25)
    max_position_percent: float = Field(default=35, gt=0, le=100)
    gap_skip_percent: float = Field(default=2, ge=0, le=10)
    regime_sma: int = Field(default=50, ge=10, le=250)

    @model_validator(mode="after")
    def _fits_together(self) -> "SwingRuleSettings":
        if self.risk_per_trade_percent > self.heat_cap_percent:
            raise ValueError("Risk a trade is larger than the open-risk cap, so not even one trade could be taken.")
        return self

    def rules(self, strategies: tuple[str, ...] | list[str] | None = None) -> SwingRules:
        def dec(value: float) -> Decimal:
            return Decimal(str(value))

        extra: dict[str, Any] = {} if strategies is None else {"strategies": tuple(strategies)}
        return SwingRules(
            capital=dec(self.capital),
            risk_per_trade_percent=dec(self.risk_per_trade_percent),
            max_open_positions=self.max_open_positions,
            max_per_sector=self.max_per_sector,
            heat_cap_percent=dec(self.heat_cap_percent),
            max_position_percent=dec(self.max_position_percent),
            gap_skip=dec(self.gap_skip_percent) / 100,
            regime_sma=self.regime_sma,
            **extra,
        )


DEFAULT_SWING_RULES = SwingRuleSettings().model_dump()


async def swing_rule_settings(session: AsyncSession) -> SwingRuleSettings:
    row = await session.get(ApplicationSetting, SWING_RULES_KEY)
    if row is None or not isinstance(row.value, dict):
        return SwingRuleSettings()
    return SwingRuleSettings.model_validate({**DEFAULT_SWING_RULES, **row.value})


async def swing_rules(session: AsyncSession, strategies: tuple[str, ...] | list[str] | None = None) -> SwingRules:
    """The rules a swing run uses: saved settings, or the defaults."""
    return (await swing_rule_settings(session)).rules(strategies)
