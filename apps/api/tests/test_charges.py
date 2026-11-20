"""U2: the two rate cards, set in Settings > Charges and used where trades are costed.

The claims that matter: the defaults change nothing on deploy; a saved card is
what the swing desk and the swing backtest then charge; each save is one
recorded change with its reason; a preview writes nothing; and figures outside
sane bounds are refused.
"""

import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from app import swing_worker
from app.api.routes import charges as routes
from app.api.routes.paper import update_controls
from app.db.models import ApplicationSetting, AuditLog, SettingRevision
from app.db.session import SessionLocal
from app.services.charges import (
    SWING_CHARGES_KEY,
    SwingChargeSettings,
    intraday_round_trip,
    swing_cost_model,
    swing_round_trip,
)
from app.services.paper_execution import PAPER_EXECUTION_KEY, PaperExecutionControls, transaction_costs
from app.services.swing_backtest import CostModel

ADMIN = SimpleNamespace(id=None)
KEYS = (PAPER_EXECUTION_KEY, SWING_CHARGES_KEY)


@pytest.fixture(autouse=True)
async def clean_cards():
    """Both cards unset for the test, and put back as they were afterwards."""
    async with SessionLocal() as session:
        backup = {key: (row.value if (row := await session.get(ApplicationSetting, key)) else None) for key in KEYS}
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key.in_(KEYS)))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key.in_(KEYS)))
        for key, value in backup.items():
            if value is not None:
                session.add(ApplicationSetting(key=key, value=value))
        await session.execute(delete(SettingRevision).where(SettingRevision.key.in_(KEYS)))
        await session.commit()


# --- the defaults are today's figures ------------------------------------------------


def test_the_default_swing_card_is_the_cost_model_the_backtest_already_used():
    assert SwingChargeSettings().cost_model() == CostModel()


async def test_with_nothing_saved_the_swing_desk_charges_the_defaults():
    async with SessionLocal() as session:
        assert await swing_cost_model(session) == CostModel()


# --- worked examples agree with the code that charges trades ------------------------


def test_the_swing_example_is_the_buy_and_the_sell_the_desk_charges():
    model, value = CostModel(), Decimal("50000")
    trip = swing_round_trip(SwingChargeSettings(), value)
    assert trip.buy == model.buy(value).quantize(Decimal("0.01"))
    assert trip.sell == model.sell(value).quantize(Decimal("0.01"))
    # The itemised lines add up to the total, to within rounding of each line.
    assert abs(sum(trip.lines.values()) - trip.total) <= Decimal("0.05")
    assert trip.lines["dp"] == Decimal("20.00")


def test_the_intraday_example_is_the_charge_on_a_buy_and_a_sell():
    controls, value = PaperExecutionControls(), Decimal("50000")
    trip = intraday_round_trip(controls, value)
    buy = transaction_costs(Decimal("1"), 50000, "BUY", controls).total
    sell = transaction_costs(Decimal("1"), 50000, "SELL", controls).total
    assert trip.total == (buy + sell).quantize(Decimal("0.01"))
    assert trip.lines["dp"] == 0
    assert trip.lines["stt"] > 0  # sell side only, but there is a sell


def test_the_intraday_brokerage_cap_holds_in_the_example():
    trip = intraday_round_trip(PaperExecutionControls(brokerage_percent=1, brokerage_cap=20), Decimal("1000000"))
    assert trip.lines["brokerage"] == Decimal("40.00")  # Rs 20 an order, two orders


# --- saving ------------------------------------------------------------------------


async def test_a_saved_swing_card_is_what_the_desk_then_charges_and_one_recorded_change():
    card = SwingChargeSettings(brokerage_per_order=0, dp_charge_per_sale=15.93)
    async with SessionLocal() as session:
        view = await routes.update_swing(
            routes.SwingChargesRequest(charges=card, reason="contract note 12 Nov"), session, ADMIN
        )
    assert view.swing.brokerage_per_order == 0
    assert view.swing_example.lines["brokerage"] == 0
    async with SessionLocal() as session:
        model = await swing_cost_model(session)
        assert model.brokerage_per_order == 0
        assert model.dp_charge_per_sale == Decimal("15.93")
        revisions = (
            await session.scalars(select(SettingRevision).where(SettingRevision.key == SWING_CHARGES_KEY))
        ).all()
        assert len(revisions) == 1
        assert sorted(revisions[0].changed_keys) == ["brokerage_per_order", "dp_charge_per_sale"]
        audit = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == "settings.swing_charges_updated")
                .order_by(AuditLog.created_at.desc())
            )
        ).first()
        assert audit.metadata_json["reason"] == "contract note 12 Nov"


async def test_a_saved_intraday_card_is_stored_and_recorded():
    async with SessionLocal() as session:
        await routes.update_intraday(
            routes.IntradayChargesRequest(charges=PaperExecutionControls(brokerage_cap=10)), session, ADMIN
        )
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
        assert row.value["brokerage_cap"] == 10
        revision = await session.scalar(select(SettingRevision).where(SettingRevision.key == PAPER_EXECUTION_KEY))
        assert revision.changed_keys == ["brokerage_cap"]


async def test_the_older_paper_controls_save_is_recorded_too():
    async with SessionLocal() as session:
        await update_controls(PaperExecutionControls(slippage_bps=5), session, ADMIN)
    async with SessionLocal() as session:
        revision = await session.scalar(select(SettingRevision).where(SettingRevision.key == PAPER_EXECUTION_KEY))
        assert revision.changed_keys == ["slippage_bps"]


async def test_a_preview_writes_nothing():
    result = await routes.preview(
        routes.PreviewRequest(swing=SwingChargeSettings(brokerage_per_order=0), trade_value=100000), ADMIN
    )
    assert result.intraday is None
    assert result.swing.trade_value == 100000
    async with SessionLocal() as session:
        assert await session.get(ApplicationSetting, SWING_CHARGES_KEY) is None


@pytest.mark.parametrize(
    "bad",
    [{"brokerage_per_order": -1}, {"dp_charge_per_sale": 5000}, {"stt_percent": 5}, {"gst_percent": 101}],
)
def test_figures_outside_sane_bounds_are_refused(bad):
    with pytest.raises(ValidationError):
        SwingChargeSettings(**bad)


def test_the_worker_reads_the_card_once_per_run_and_passes_it_on():
    source = inspect.getsource(swing_worker.process)
    assert "costs = await swing_cost_model(session)" in source
    assert "costs=costs" in source
