"""The desk: what it is, what follows from it, and that intraday is unchanged by it.

Step 0.1 of the swing desk adds the desk to the data and states its rules, and
wires nothing into the order path. The tests that matter most here are the ones
pinning intraday to exactly its old behaviour, because the next step replaces
``product_for`` and the INTRADAY fallbacks with these functions.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import LiveOrderSubmission, PaperSignal
from app.db.session import SessionLocal
from app.services.broker_adapter import DELIVERY, INTRADAY
from app.services.desks import (
    INTRADAY_DESK,
    SWING_DESK,
    ExitProductUnknownError,
    UnknownDeskError,
    counts_toward_daily_ceilings,
    exit_product,
    normalise_desk,
    product_for_desk,
    side_allowed,
    squared_off_by_session_close,
)
from app.services.live_shadow import product_for

NOW = datetime(2026, 11, 20, 4, 30, tzinfo=UTC)


# -- intraday is exactly what it was ------------------------------------------


@pytest.mark.parametrize("leverage", [True, False])
@pytest.mark.parametrize("desk", [INTRADAY_DESK, None, "", "intraday"])
def test_the_intraday_desk_gets_the_product_it_always_got(desk, leverage):
    assert product_for_desk(desk, intraday_leverage_enabled=leverage) == product_for(leverage)


def test_the_intraday_exit_fallback_is_still_intraday():
    chosen = exit_product(INTRADAY_DESK, None)
    assert chosen.product == INTRADAY
    assert chosen.recorded is False


def test_the_intraday_desk_still_trades_both_sides_and_is_squared_off():
    assert side_allowed(INTRADAY_DESK, "LONG")
    assert side_allowed(INTRADAY_DESK, "SHORT")
    assert squared_off_by_session_close(INTRADAY_DESK)
    assert counts_toward_daily_ceilings(INTRADAY_DESK)


# -- the swing desk ------------------------------------------------------------


@pytest.mark.parametrize("leverage", [True, False])
def test_a_swing_entry_is_delivery_whatever_the_leverage_switch_says(leverage):
    assert product_for_desk(SWING_DESK, intraday_leverage_enabled=leverage) == DELIVERY


def test_a_swing_exit_with_no_recorded_product_is_refused_not_guessed():
    with pytest.raises(ExitProductUnknownError, match="short"):
        exit_product(SWING_DESK, None)


def test_a_recorded_product_is_used_on_either_desk():
    assert exit_product(SWING_DESK, DELIVERY).product == DELIVERY
    assert exit_product(SWING_DESK, DELIVERY).recorded is True
    assert exit_product(INTRADAY_DESK, DELIVERY).product == DELIVERY


def test_the_swing_desk_is_long_only_kept_overnight_and_outside_the_daily_ceilings():
    assert side_allowed(SWING_DESK, "LONG")
    assert not side_allowed(SWING_DESK, "SHORT")
    assert not squared_off_by_session_close(SWING_DESK)
    assert not counts_toward_daily_ceilings(SWING_DESK)


# -- names ---------------------------------------------------------------------


def test_a_missing_desk_is_intraday_and_a_misspelt_one_is_refused():
    assert normalise_desk(None) == INTRADAY_DESK
    assert normalise_desk(" swing ") == SWING_DESK
    with pytest.raises(UnknownDeskError):
        normalise_desk("SWNG")
    with pytest.raises(UnknownDeskError):
        product_for_desk("SWNG", intraday_leverage_enabled=False)


# -- the column ------------------------------------------------------------------


def _signal(desk: str | None = None) -> PaperSignal:
    signal = PaperSignal(
        signal_key=uuid4().hex,
        instrument_token="NSE_EQ|INE397D01024",
        session_date=NOW.date(),
        candle_opened_at=NOW,
        strategy_version="rs-pullback-v1@1",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        target_price=Decimal("102"),
        quantity=10,
        risk_amount=Decimal("10"),
        score=80,
    )
    if desk is not None:
        signal.desk = desk
    return signal


def _order() -> LiveOrderSubmission:
    return LiveOrderSubmission(
        client_order_id=uuid4().hex[:40],
        exchange="NSE_EQ",
        trading_symbol="NSE_EQ|INE397D01024",
        product="I",
        price_type="MARKET",
        transaction_type="BUY",
        quantity=1,
        request_snapshot={},
    )


async def test_a_row_written_without_a_desk_is_intraday():
    async with SessionLocal() as session:
        signal = _signal()
        order = _order()
        session.add_all([signal, order])
        await session.flush()
        assert signal.desk == INTRADAY_DESK
        assert order.desk == INTRADAY_DESK
        await session.rollback()


async def test_a_row_inserted_by_code_that_predates_the_desk_is_intraday():
    """The server default, not the ORM default: what the running image will write."""
    async with SessionLocal() as session:
        key = uuid4().hex[:40]
        await session.execute(
            text(
                "INSERT INTO live_order_submissions (id, client_order_id, exchange, trading_symbol, product, price_type, "
                "transaction_type, quantity, status, broker_order_numbers, request_snapshot, created_at) "
                "VALUES (:id, :key, 'NSE_EQ', 'NSE_EQ|INE397D01024', 'I', 'MARKET', 'BUY', 1, 'PREPARED', '[]', '{}', now())"
            ),
            {"id": uuid4(), "key": key},
        )
        desk = await session.scalar(select(LiveOrderSubmission.desk).where(LiveOrderSubmission.client_order_id == key))
        assert desk == INTRADAY_DESK
        await session.rollback()


async def test_the_database_accepts_swing_and_refuses_a_misspelt_desk():
    async with SessionLocal() as session:
        swing = _signal(SWING_DESK)
        session.add(swing)
        await session.flush()
        assert swing.desk == SWING_DESK
        await session.rollback()

    async with SessionLocal() as session:
        session.add(_signal("SWNG"))
        with pytest.raises(IntegrityError, match="ck_paper_signals_desk"):
            await session.flush()
        await session.rollback()


# -- the on/off switches (step 0.4a) -------------------------------------------


def test_with_nothing_saved_intraday_is_on_and_swing_is_off():
    """What every deployment did before the desks existed, so a deploy changes nothing."""
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls
    from app.services.desks import desk_enabled

    assert desk_enabled({}, INTRADAY_DESK) is True
    assert desk_enabled({}, SWING_DESK) is False
    saved = TradingControls.model_validate(DEFAULT_TRADING_CONTROLS)
    assert desk_enabled(saved, INTRADAY_DESK) is True
    assert desk_enabled(saved, SWING_DESK) is False


def test_a_switch_is_read_from_a_dict_or_a_model():
    from app.services.desks import desk_enabled

    assert desk_enabled({"intraday_desk_enabled": False}, INTRADAY_DESK) is False
    assert desk_enabled({"swing_desk_enabled": True}, SWING_DESK) is True


def test_saving_one_change_over_a_row_that_predates_the_switches_records_one_change():
    """A row saved before the switches existed must not make the next edit look like three.

    Section D of the daily review flags two settings moved in one session.
    """
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls, _as_saved
    from app.services.settings_history import summarise

    stored = {
        k: v for k, v in DEFAULT_TRADING_CONTROLS.items() if k not in ("intraday_desk_enabled", "swing_desk_enabled")
    }
    edited = TradingControls.model_validate({**stored, "daily_loss_limit": 750.0}).model_dump()
    assert summarise(_as_saved(stored), edited).changed_keys == ["daily_loss_limit"]
    # And the bare comparison it replaces really did report three.
    assert len(summarise(stored, edited).changed_keys) == 3
