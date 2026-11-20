"""An order records its desk, on the write-ahead row and through an approval.

Exits and stops will choose their product from this column (step 0.2, change
2). A swing order recorded as intraday would be closed by the intraday exits at
the square-off, so the desk must survive both ways an entry reaches the row:
directly, and rebuilt from an approval the operator answered minutes later.
"""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.db.session import SessionLocal
from app.services.broker_adapter import BrokerOrderDescription
from app.services.live_approval import _request_from
from app.services.live_orders import LiveOrderRequest, prepare_submission

DESCRIPTION = BrokerOrderDescription(
    resolved=True,
    exchange="NSE_EQ",
    symbol="NSE_EQ|INE335Y01020",
    side="BUY",
    product="D",
    order_type="MARKET",
    validity="DAY",
)


def _request(**extra) -> LiveOrderRequest:
    return LiveOrderRequest(
        instrument_token="NSE_EQ|INE335Y01020",
        side="BUY",
        quantity=4,
        order_type="MARKET",
        product="DELIVERY",
        price=Decimal("412.40"),
        **extra,
    )


async def _recorded_desk(request: LiveOrderRequest) -> str:
    async with SessionLocal() as session:
        record = await prepare_submission(
            session, request, DESCRIPTION, broker="UPSTOX", client_order_id=uuid4().hex[:40]
        )
        await session.flush()
        desk = record.desk
        await session.rollback()
    return desk


async def test_a_request_with_no_desk_is_recorded_as_intraday():
    assert await _recorded_desk(_request()) == "INTRADAY"


async def test_a_swing_request_is_recorded_as_swing():
    assert await _recorded_desk(_request(desk="SWING")) == "SWING"


def _approval() -> SimpleNamespace:
    return SimpleNamespace(
        instrument_token="NSE_EQ|INE335Y01020",
        transaction_type="BUY",
        quantity=4,
        price_type="MARKET",
        product="DELIVERY",
        price=Decimal("412.40"),
    )


def test_an_approval_rebuilt_without_a_signal_is_intraday():
    assert _request_from(_approval()).desk == "INTRADAY"


def test_an_approval_rebuilt_from_a_swing_signal_stays_swing():
    assert _request_from(_approval(), "SWING").desk == "SWING"
