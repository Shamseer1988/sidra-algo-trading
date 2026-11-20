"""What a trade costs, as the operator set it in Settings > Charges.

Two rate cards, because the broker charges the two desks differently:

* **Intraday** (``paper_execution_controls``): brokerage as a percentage with a
  cap per order, STT on the sell side, and the paper desk's fill assumptions
  (slippage, participation). Used by intraday paper fills, backtests, sweeps
  and the rate-card estimate on live trades in history.
* **Swing delivery** (``swing_delivery_charges``): brokerage per order, STT on
  both sides, and the depository (DP) charge on each sale. Used by the swing
  paper desk and the swing backtest.

Neither card is read by order placement or by a live risk gate. A wrong figure
makes a report or a backtest wrong, not an order.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting
from app.services.paper_execution import (
    DEFAULT_PAPER_EXECUTION_CONTROLS,
    PAPER_EXECUTION_KEY,
    PaperExecutionControls,
    transaction_costs,
)
from app.services.swing_backtest import CostModel

SWING_CHARGES_KEY = "swing_delivery_charges"
PAISA = Decimal("0.01")


class SwingChargeSettings(BaseModel):
    """Equity delivery charges, in the units a contract note prints them.

    Percentages are percent (0.1 means 0.1%), as on the broker's rate card.
    The defaults are the published NSE delivery rates and a Rs 20 plan; the
    brokerage and DP figures depend on the broker plan and should be checked
    against a real contract note.
    """

    brokerage_per_order: float = Field(default=20, ge=0, le=1_000)
    stt_percent: float = Field(default=0.1, ge=0, le=1)
    exchange_transaction_percent: float = Field(default=0.00297, ge=0, le=1)
    sebi_percent: float = Field(default=0.0001, ge=0, le=1)
    stamp_duty_buy_percent: float = Field(default=0.015, ge=0, le=1)
    gst_percent: float = Field(default=18, ge=0, le=100)
    dp_charge_per_sale: float = Field(default=20, ge=0, le=1_000)

    def cost_model(self) -> CostModel:
        def rate(percent: float) -> Decimal:
            return Decimal(str(percent)) / Decimal("100")

        return CostModel(
            brokerage_per_order=Decimal(str(self.brokerage_per_order)),
            stt_rate=rate(self.stt_percent),
            exchange_rate=rate(self.exchange_transaction_percent),
            sebi_rate=rate(self.sebi_percent),
            stamp_rate=rate(self.stamp_duty_buy_percent),
            gst_rate=rate(self.gst_percent),
            dp_charge_per_sale=Decimal(str(self.dp_charge_per_sale)),
        )


DEFAULT_SWING_CHARGES = SwingChargeSettings().model_dump()


async def intraday_charges(session: AsyncSession) -> PaperExecutionControls:
    row = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    return PaperExecutionControls.model_validate(
        row.value if row and isinstance(row.value, dict) else DEFAULT_PAPER_EXECUTION_CONTROLS
    )


async def swing_charges(session: AsyncSession) -> SwingChargeSettings:
    row = await session.get(ApplicationSetting, SWING_CHARGES_KEY)
    return SwingChargeSettings.model_validate(
        row.value if row and isinstance(row.value, dict) else DEFAULT_SWING_CHARGES
    )


async def swing_cost_model(session: AsyncSession) -> CostModel:
    return (await swing_charges(session)).cost_model()


# --- worked examples, so a figure is checked against a contract note, not trusted ---


@dataclass(frozen=True)
class RoundTrip:
    """One buy and one sell of the same value, and what each leg costs."""

    trade_value: Decimal
    buy: Decimal
    sell: Decimal
    lines: dict[str, Decimal]

    @property
    def total(self) -> Decimal:
        return self.buy + self.sell

    def as_dict(self) -> dict:
        data = asdict(self)
        data["total"] = self.total
        return data


def _paisa(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def intraday_round_trip(controls: PaperExecutionControls, trade_value: Decimal) -> RoundTrip:
    """Buy then sell ``trade_value`` intraday, one order each, at a price of Rs 1 a share.

    The price only scales the quantity; every intraday charge is a percentage of
    the value or a cap on one, so the figure is the same at any price.
    """
    quantity = int(trade_value)
    buy = transaction_costs(Decimal("1"), quantity, "BUY", controls)
    sell = transaction_costs(Decimal("1"), quantity, "SELL", controls)
    lines = {
        "brokerage": buy.brokerage + sell.brokerage,
        "stt": buy.stt + sell.stt,
        "exchange": buy.exchange_charge + sell.exchange_charge,
        "gst": buy.gst + sell.gst,
        "sebi": buy.sebi_charge + sell.sebi_charge,
        "stamp_duty": buy.stamp_duty + sell.stamp_duty,
        "dp": Decimal("0"),
    }
    return RoundTrip(
        trade_value=trade_value,
        buy=_paisa(buy.total),
        sell=_paisa(sell.total),
        lines={key: _paisa(value) for key, value in lines.items()},
    )


def swing_round_trip(settings: SwingChargeSettings, trade_value: Decimal) -> RoundTrip:
    """Buy ``trade_value`` for delivery and sell it on a later day: one order each."""
    model = settings.cost_model()
    value = Decimal(trade_value)
    fees = value * (model.exchange_rate + model.sebi_rate)
    lines = {
        "brokerage": model.brokerage_per_order * 2,
        "stt": value * model.stt_rate * 2,
        "exchange": value * model.exchange_rate * 2,
        "gst": (model.brokerage_per_order + fees) * model.gst_rate * 2,
        "sebi": value * model.sebi_rate * 2,
        "stamp_duty": value * model.stamp_rate,
        "dp": model.dp_charge_per_sale,
    }
    return RoundTrip(
        trade_value=value,
        buy=_paisa(model.buy(value)),
        sell=_paisa(model.sell(value)),
        lines={key: _paisa(amount) for key, amount in lines.items()},
    )
