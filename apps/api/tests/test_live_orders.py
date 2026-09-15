"""Placing an order, and classifying what came back.

Only one mistake here is expensive in both directions. Calling a timeout a
rejection produces a second live order on top of a first that may already exist;
calling it an acceptance produces a position the risk engine sizes against and
nobody can cancel. So the tests weight UNKNOWN heavily, and the happy path
lightly.
"""

from decimal import Decimal

import pytest

from app.services.firstock.client import FirstockError
from app.services.firstock.orders import (
    FirstockApiError,
    FirstockAuthError,
    FirstockOrderClient,
    FirstockTransportUnknown,
    cancellation_confirmed,
)
from app.services.live_orders import (
    ACCEPTED,
    REJECTED,
    UNKNOWN,
    LiveOrderRequest,
    _order_numbers,
    new_client_order_id,
    send_prepared_order,
)


class FakeRecord:
    def __init__(self, client_order_id: str = "sidra-abc") -> None:
        self.client_order_id = client_order_id


class FakeClient:
    def __init__(self, data=None, raises: Exception | None = None) -> None:
        self._data = data
        self._raises = raises
        self.payloads: list[dict] = []

    async def place_order(self, **kwargs: object) -> object:
        self.payloads.append(dict(kwargs))
        if self._raises:
            raise self._raises
        return self._data


REQUEST = LiveOrderRequest(
    exchange="NSE",
    trading_symbol="IDEA-EQ",
    product="I",
    price_type="LMT",
    transaction_type="B",
    quantity=10,
    price=Decimal("418"),
)


# --- identity -------------------------------------------------------------


def test_client_order_ids_are_unique_and_fit_a_telegram_callback() -> None:
    ids = {new_client_order_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(value) <= 40 for value in ids)


# --- outcome classification ----------------------------------------------


async def test_an_order_number_means_accepted() -> None:
    client = FakeClient({"orderNumber": "24091500001"})
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == ACCEPTED
    assert outcome.broker_order_numbers == ["24091500001"]


async def test_a_timeout_is_unknown_and_never_a_rejection() -> None:
    """The request body was sent. The order may exist."""
    client = FakeClient(raises=FirstockTransportUnknown("placeOrder timed out"))
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == UNKNOWN
    assert outcome.is_unknown is True


async def test_a_documented_refusal_is_a_rejection_and_keeps_its_code() -> None:
    client = FakeClient(raises=FirstockApiError("insufficient funds", code="400", name="BAD_REQUEST", field="price"))
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == REJECTED
    assert outcome.failure_code == "400"
    assert outcome.failure_name == "BAD_REQUEST"


async def test_a_rejected_session_token_is_a_rejection_not_an_unknown() -> None:
    """The broker answers this before an order exists, so nothing was placed."""
    client = FakeClient(raises=FirstockAuthError("session token rejected"))
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == REJECTED
    assert outcome.failure_name == "INVALID_JKEY"


async def test_success_without_an_order_number_is_unknown() -> None:
    """An order we cannot name is an order we cannot cancel."""
    client = FakeClient({"requestTime": "10:15:00"})
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == UNKNOWN


async def test_the_send_never_retries_on_its_own() -> None:
    """A retry is a second order whenever the first one may already be live."""
    client = FakeClient(raises=FirstockTransportUnknown("timeout"))
    await send_prepared_order(client, FakeRecord(), REQUEST)
    assert len(client.payloads) == 1


async def test_the_client_order_id_travels_to_the_broker_as_remarks() -> None:
    """Without it, a lost response cannot be resolved against the order book."""
    client = FakeClient({"orderNumber": "1"})
    await send_prepared_order(client, FakeRecord("sidra-deadbeef"), REQUEST)
    assert client.payloads[0]["remarks"] == "sidra-deadbeef"


# --- slicing --------------------------------------------------------------


def test_every_slice_is_captured() -> None:
    """A schema that kept one number would silently lose real exposure."""
    data = {"orders": [{"orderNumber": "1"}, {"orderNumber": "2"}, {"orderNumber": "3"}]}
    assert _order_numbers(data) == ["1", "2", "3"]


def test_a_repeated_number_counts_once() -> None:
    assert _order_numbers([{"orderNumber": "7"}, {"orderNumber": "7"}]) == ["7"]


@pytest.mark.parametrize("data", [None, {}, [], {"orderNumber": ""}, {"orderNumber": None}, "nonsense"])
def test_shapes_carrying_no_number_yield_none(data: object) -> None:
    assert _order_numbers(data) == []


async def test_a_sliced_response_is_accepted_with_all_its_numbers() -> None:
    client = FakeClient([{"orderNumber": "a"}, {"orderNumber": "b"}])
    outcome = await send_prepared_order(client, FakeRecord(), REQUEST)
    assert outcome.status == ACCEPTED
    assert outcome.broker_order_numbers == ["a", "b"]


# --- the adapter's own guards --------------------------------------------


async def test_placing_without_a_client_order_id_is_refused() -> None:
    """The recovery key is not optional, whatever the API would accept."""
    client = FirstockOrderClient.__new__(FirstockOrderClient)
    with pytest.raises(FirstockError, match="recovery key"):
        await FirstockOrderClient.place_order(
            client,
            exchange="NSE",
            trading_symbol="IDEA-EQ",
            product="I",
            price_type="LMT",
            transaction_type="B",
            retention="DAY",
            quantity="1",
            price="418",
            remarks="",
        )


@pytest.mark.parametrize("method", ["cancel_order", "modify_order"])
async def test_changing_an_order_requires_an_order_number(method: str) -> None:
    client = FirstockOrderClient.__new__(FirstockOrderClient)
    kwargs = {"order_number": ""}
    if method == "modify_order":
        kwargs |= {
            "exchange": "NSE",
            "trading_symbol": "IDEA-EQ",
            "product": "I",
            "price_type": "LMT",
            "retention": "DAY",
            "quantity": "1",
            "price": "418",
        }
    with pytest.raises(FirstockError, match="order_number is required"):
        await getattr(FirstockOrderClient, method)(client, **kwargs)


# --- cancellation is not confirmed by its envelope -----------------------


def test_a_cancellation_carrying_a_reject_reason_is_not_a_cancellation() -> None:
    """The documented response says success while refusing in rejreason."""
    confirmed, detail = cancellation_confirmed({"orderNumber": "1", "rejreason": "Order is not open"})
    assert confirmed is False
    assert "not open" in detail


def test_a_cancellation_without_an_order_number_is_not_confirmed() -> None:
    confirmed, _ = cancellation_confirmed({})
    assert confirmed is False


def test_a_clean_cancellation_still_only_claims_the_request_was_accepted() -> None:
    confirmed, detail = cancellation_confirmed({"orderNumber": "1", "rejreason": ""})
    assert confirmed is True
    assert "confirm from the order book" in detail
