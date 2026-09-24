"""What the risk settings actually allow, once they are read together.

Six controls govern how much the account may trade, and they interact. Set
independently they can contradict each other silently, which is what happened:
1% of ₹10,000 is ₹100 of planned risk per trade against a 2% daily budget of
₹200, so the third and fourth trades were refused however high the trade ceiling
was set. The operator read "maximum 4 trades", got 2, and the system never said
why.

Two ways to deal with that, and this module takes the second.

The first is to refuse the combination. That would also refuse legitimate ones —
a deliberately tight daily budget under a generous trade ceiling is a reasonable
thing to want, and a validator cannot tell it apart from a mistake.

The second is to **compute what the settings mean and say so**. The effective
trade capacity is derived, the control that binds it is named, and anything one
control quietly overrides in another is reported as a warning rather than
discovered from an empty session. Nothing is rejected; everything is explained.

Two vocabularies are kept apart deliberately, because conflating them is how a
ceiling comes to be read as a promise:

    **planned risk**  what a trade is designed to lose if its stop is hit. It is
                      an intention. Slippage, a gap, or a stop that cannot be
                      placed all break it.
    **realised P&L**  what the account actually made or lost, after charges.
                      The daily stop is measured on this.

And **exposure is not cash.** ``maximum_open_exposure_percent`` is a percent of
capital multiplied by the leverage multiplier, so 100% at 5x on ₹10,000 of cash
is ₹50,000 of exposure. Labelling that as capital would overstate the account
fivefold.
"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

# The two profiles Shamseer specified. Stored here rather than in the UI so the
# backend stays authoritative: applying a preset writes these numbers through
# the same validation and audit path as any other edit.
CAUTIOUS_PAPER_START = "CAUTIOUS_PAPER_START"
USER_ADVANCED = "USER_ADVANCED"

RISK_PRESETS: dict[str, dict[str, Any]] = {
    CAUTIOUS_PAPER_START: {
        "label": "Cautious paper start",
        "description": "₹100 planned risk a trade, ₹400 daily loss stop. Where to begin.",
        "controls": {
            "account_capital": 10000.0,
            "risk_per_trade_percent": 1.0,
            "maximum_daily_risk_percent": 4.0,
            "maximum_daily_trades": 4,
            "maximum_open_positions": 1,
            "daily_loss_limit": 400.0,
            "daily_profit_target": 2000.0,
        },
    },
    USER_ADVANCED: {
        "label": "User advanced profile",
        "description": "₹250 planned risk a trade, ₹1,000 daily loss stop — 10% of ₹10,000.",
        "controls": {
            "account_capital": 10000.0,
            "risk_per_trade_percent": 2.5,
            "maximum_daily_risk_percent": 10.0,
            "maximum_daily_trades": 4,
            "maximum_open_positions": 1,
            "daily_loss_limit": 1000.0,
            "daily_profit_target": 2000.0,
        },
    },
}


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _percent_of(amount: Decimal, capital: Decimal) -> Decimal | None:
    """What fraction of capital an amount is, or None when capital is unusable."""
    if capital <= 0:
        return None
    return (amount * Decimal("100") / capital).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class EffectiveLimits:
    """The settings as the engine will actually apply them."""

    capital: Decimal
    planned_risk_per_trade: Decimal
    daily_risk_budget: Decimal
    trades_the_budget_allows: int
    configured_trade_ceiling: int
    effective_trade_ceiling: int
    binding_control: str
    maximum_open_positions: int
    exposure_ceiling: Decimal
    leverage_multiplier: Decimal
    daily_loss_limit: Decimal
    daily_loss_percent: Decimal | None
    daily_profit_target: Decimal
    daily_profit_percent: Decimal | None
    warnings: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """Serialisable, for the settings API and the audit record."""
        return {
            "capital": str(self.capital),
            "planned_risk_per_trade": str(self.planned_risk_per_trade),
            "daily_risk_budget": str(self.daily_risk_budget),
            "trades_the_budget_allows": self.trades_the_budget_allows,
            "configured_trade_ceiling": self.configured_trade_ceiling,
            "effective_trade_ceiling": self.effective_trade_ceiling,
            "binding_control": self.binding_control,
            "maximum_open_positions": self.maximum_open_positions,
            "exposure_ceiling": str(self.exposure_ceiling),
            "leverage_multiplier": str(self.leverage_multiplier),
            "daily_loss_limit": str(self.daily_loss_limit),
            "daily_loss_percent": str(self.daily_loss_percent) if self.daily_loss_percent is not None else None,
            "daily_profit_target": str(self.daily_profit_target),
            "daily_profit_percent": str(self.daily_profit_percent) if self.daily_profit_percent is not None else None,
            "warnings": list(self.warnings),
        }


def effective_limits(controls: Any) -> EffectiveLimits:
    """Read the controls together and report what they permit.

    ``controls`` may be the Pydantic model or a plain dict; both are read the
    same way so the settings route and a stored row cannot diverge.
    """

    def value(name: str, default: str = "0") -> Decimal:
        if isinstance(controls, dict):
            return _decimal(controls.get(name), default)
        return _decimal(getattr(controls, name, None), default)

    capital = value("account_capital")
    risk_percent = value("risk_per_trade_percent")
    daily_risk_percent = value("maximum_daily_risk_percent")
    configured_ceiling = int(value("maximum_daily_trades", "0"))
    open_positions = int(value("maximum_open_positions", "0"))
    exposure_percent = value("maximum_open_exposure_percent")
    leverage_on = bool(
        controls.get("intraday_leverage_enabled")
        if isinstance(controls, dict)
        else getattr(controls, "intraday_leverage_enabled", False)
    )
    leverage = value("intraday_leverage_multiplier", "1") if leverage_on else Decimal("1")
    loss_limit = value("daily_loss_limit")
    profit_target = value("daily_profit_target")

    planned_risk = _money(capital * risk_percent / Decimal("100"))
    daily_budget = _money(capital * daily_risk_percent / Decimal("100"))

    # Whole trades only: a budget that funds two and a half trades funds two.
    if planned_risk > 0:
        budget_allows = int((daily_budget / planned_risk).to_integral_value(rounding=ROUND_DOWN))
    else:
        budget_allows = configured_ceiling

    effective_ceiling = min(configured_ceiling, budget_allows) if configured_ceiling else budget_allows
    if configured_ceiling and budget_allows < configured_ceiling:
        binding = "maximum_daily_risk_percent"
    else:
        binding = "maximum_daily_trades"

    exposure_ceiling = _money(capital * exposure_percent * leverage / Decimal("100"))

    warnings: list[str] = []
    if configured_ceiling and budget_allows < configured_ceiling:
        warnings.append(
            f"The daily risk budget of ₹{daily_budget} funds {budget_allows} trade(s) at "
            f"₹{planned_risk} of planned risk each, so only {budget_allows} of the "
            f"{configured_ceiling} configured trades can be taken. Raise "
            f"maximum_daily_risk_percent to {(_money(planned_risk * configured_ceiling * Decimal('100') / capital)) if capital > 0 else 0}% "
            "to allow all of them."
        )
    if loss_limit > 0 and planned_risk > 0 and loss_limit < planned_risk:
        warnings.append(
            f"The daily loss stop of ₹{loss_limit} is smaller than one trade's planned risk of "
            f"₹{planned_risk}, so a single losing trade ends the day."
        )
    if loss_limit <= 0:
        warnings.append("No daily loss stop is set. The day has no money-based floor.")
    if profit_target <= 0:
        warnings.append("No daily profit stop is set. The day has no money-based ceiling.")
    if open_positions > 1 and effective_ceiling > 0:
        warnings.append(
            f"Up to {open_positions} positions may be open at once, so more than one stop can be hit by the same move."
        )

    return EffectiveLimits(
        capital=capital,
        planned_risk_per_trade=planned_risk,
        daily_risk_budget=daily_budget,
        trades_the_budget_allows=budget_allows,
        configured_trade_ceiling=configured_ceiling,
        effective_trade_ceiling=max(0, effective_ceiling),
        binding_control=binding,
        maximum_open_positions=open_positions,
        exposure_ceiling=exposure_ceiling,
        leverage_multiplier=leverage,
        daily_loss_limit=loss_limit,
        daily_loss_percent=_percent_of(loss_limit, capital),
        daily_profit_target=profit_target,
        daily_profit_percent=_percent_of(profit_target, capital),
        warnings=warnings,
    )
