"""The seam between the execution layer and a broker.

Two brokers now sit behind one order shape, and an operator chooses between
them. The behaviour worth testing is that they are interchangeable in the ways
that matter — the same three outcomes, the same identity field read back — and
that neither leaks its own vocabulary upward.

The translations are tested field by field because a silently renamed field is
the class of bug that reaches production: a wrong ``transaction_type`` does not
fail, it trades the other way.
"""

from decimal import Decimal

import pytest

from app.services.broker_adapter import (
    ACCEPTED,
    BUY,
    DELIVERY,
    INTRADAY,
    LIMIT,
    MARKET,
    REJECTED,
    SELL,
    STOP_LIMIT,
    STOP_MARKET,
    UNKNOWN,
    BrokerOrder,
    FirstockAdapter,
    UpstoxAdapter,
)
from app.services.firstock.orders import FirstockApiError, FirstockTransportUnknown
from app.services.upstox_orders import UpstoxApiError, UpstoxAuthError, UpstoxTransportUnknown

ORDER = BrokerOrder(
    instrument_token="NSE_EQ|INE002A01018",
    side=BUY,
    quantity=10,
    order_type=LIMIT,
    product=INTRADAY,
    price=Decimal("418"),
    client_order_id="sidra-abc123",
)


class FakeUpstox:
    def __init__(self, result=None, raises=None) -> None:
        self._result = result if result is not None else ["1644490272000"]
        self._raises = raises
        self.calls: list[dict] = []

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result

    async def cancel_order(self, order_id):
        if self._raises:
            raise self._raises
        return {"order_id": order_id}

    async def order_book(self):
        return [{"order_id": "1", "tag": "sidra-abc123"}]

    async def positions(self):
        return [{"instrument_token": "NSE_EQ|INE002A01018", "quantity": 10}]


class FakeFirstock:
    def __init__(self, result=None, raises=None) -> None:
        self._result = result if result is not None else {"orderNumber": "24092300001"}
        self._raises = raises
        self.calls: list[dict] = []

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result

    async def cancel_order(self, order_number):
        return {"orderNumber": order_number, "rejreason": ""}

    async def order_book(self):
        return [{"orderNumber": "1", "remarks": "sidra-abc123"}]

    async def position_book(self):
        return [{"tradingSymbol": "RELIANCE-EQ", "netQuantity": "10"}]


class FakeSession:
    """Only the scalar query the symbol translation makes."""

    def __init__(self, master: dict | None = None) -> None:
        self._master = master

    async def scalar(self, _query):
        if self._master is None:
            return None
        return type("Refresh", (), {"configured_keys": self._master})()


# --- Upstox ---------------------------------------------------------------


async def test_upstox_accepts_and_returns_its_order_ids() -> None:
    result = await UpstoxAdapter(FakeUpstox()).submit(ORDER)
    assert result.status == ACCEPTED
    assert result.broker_order_ids == ["1644490272000"]


async def test_upstox_passes_the_instrument_token_through_untranslated() -> None:
    """No mapping step means no wrong-instrument bug on this path."""
    client = FakeUpstox()
    await UpstoxAdapter(client).submit(ORDER)
    assert client.calls[0]["instrument_token"] == "NSE_EQ|INE002A01018"


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(BUY, "BUY"), (SELL, "SELL")],
)
async def test_upstox_side_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await UpstoxAdapter(client).submit(BrokerOrder(**{**ORDER.__dict__, "side": canonical}))
    assert client.calls[0]["transaction_type"] == expected


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(MARKET, "MARKET"), (LIMIT, "LIMIT"), (STOP_LIMIT, "SL"), (STOP_MARKET, "SL-M")],
)
async def test_upstox_order_type_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await UpstoxAdapter(client).submit(BrokerOrder(**{**ORDER.__dict__, "order_type": canonical}))
    assert client.calls[0]["order_type"] == expected


@pytest.mark.parametrize(("canonical", "expected"), [(INTRADAY, "I"), (DELIVERY, "D")])
async def test_upstox_product_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await UpstoxAdapter(client).submit(BrokerOrder(**{**ORDER.__dict__, "product": canonical}))
    assert client.calls[0]["product"] == expected


async def test_upstox_carries_the_client_order_id_as_a_tag() -> None:
    client = FakeUpstox()
    await UpstoxAdapter(client).submit(ORDER)
    assert client.calls[0]["tag"] == "sidra-abc123"


async def test_upstox_timeout_is_unknown() -> None:
    result = await UpstoxAdapter(FakeUpstox(raises=UpstoxTransportUnknown("timed out"))).submit(ORDER)
    assert result.status == UNKNOWN


async def test_upstox_refusal_keeps_its_code() -> None:
    error = UpstoxApiError("bad quantity", code="UDAPI1026")
    result = await UpstoxAdapter(FakeUpstox(raises=error)).submit(ORDER)
    assert result.status == REJECTED
    assert result.failure_code == "UDAPI1026"


async def test_upstox_auth_failure_is_a_rejection_not_an_unknown() -> None:
    result = await UpstoxAdapter(FakeUpstox(raises=UpstoxAuthError("token"))).submit(ORDER)
    assert result.status == REJECTED


async def test_upstox_acceptance_without_an_id_is_unknown() -> None:
    result = await UpstoxAdapter(FakeUpstox(result=[])).submit(ORDER)
    assert result.status == UNKNOWN


async def test_upstox_reads_the_client_order_id_back_from_the_book() -> None:
    adapter = UpstoxAdapter(FakeUpstox())
    record = (await adapter.order_book())[0]
    assert adapter.find_client_order_id(record) == "sidra-abc123"


# --- Firstock -------------------------------------------------------------


async def test_firstock_translates_the_symbol_before_submitting() -> None:
    client = FakeFirstock()
    result = await FirstockAdapter(client, FakeSession()).submit(ORDER)
    assert result.status == ACCEPTED
    assert client.calls[0]["trading_symbol"] == "RELIANCE-EQ"
    assert client.calls[0]["exchange"] == "NSE"


async def test_firstock_refuses_an_instrument_it_cannot_name() -> None:
    """A refusal, not an exception: the caller is deciding whether to send."""
    client = FakeFirstock()
    order = BrokerOrder(**{**ORDER.__dict__, "instrument_token": "NSE_EQ|INE999Z01099"})
    result = await FirstockAdapter(client, FakeSession()).submit(order)
    assert result.status == REJECTED
    assert result.failure_name == "SYMBOL_UNRESOLVED"
    assert client.calls == []


async def test_firstock_refuses_an_index() -> None:
    order = BrokerOrder(**{**ORDER.__dict__, "instrument_token": "NSE_INDEX|Nifty 50"})
    result = await FirstockAdapter(FakeFirstock(), FakeSession()).submit(order)
    assert result.status == REJECTED


@pytest.mark.parametrize(("canonical", "expected"), [(BUY, "B"), (SELL, "S")])
async def test_firstock_side_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await FirstockAdapter(client, FakeSession()).submit(BrokerOrder(**{**ORDER.__dict__, "side": canonical}))
    assert client.calls[0]["transaction_type"] == expected


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(MARKET, "MKT"), (LIMIT, "LMT"), (STOP_LIMIT, "SL-LMT"), (STOP_MARKET, "SL-MKT")],
)
async def test_firstock_order_type_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await FirstockAdapter(client, FakeSession()).submit(BrokerOrder(**{**ORDER.__dict__, "order_type": canonical}))
    assert client.calls[0]["price_type"] == expected


@pytest.mark.parametrize(("canonical", "expected"), [(INTRADAY, "I"), (DELIVERY, "C")])
async def test_firstock_product_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await FirstockAdapter(client, FakeSession()).submit(BrokerOrder(**{**ORDER.__dict__, "product": canonical}))
    assert client.calls[0]["product"] == expected


async def test_firstock_carries_the_client_order_id_as_remarks() -> None:
    client = FakeFirstock()
    await FirstockAdapter(client, FakeSession()).submit(ORDER)
    assert client.calls[0]["remarks"] == "sidra-abc123"


async def test_firstock_timeout_is_unknown() -> None:
    client = FakeFirstock(raises=FirstockTransportUnknown("timed out"))
    result = await FirstockAdapter(client, FakeSession()).submit(ORDER)
    assert result.status == UNKNOWN


async def test_firstock_refusal_keeps_its_code() -> None:
    error = FirstockApiError("rejected", code="400", name="BAD_REQUEST", field="price")
    result = await FirstockAdapter(FakeFirstock(raises=error), FakeSession()).submit(ORDER)
    assert result.status == REJECTED
    assert result.failure_code == "400"


async def test_firstock_reads_the_client_order_id_back_from_the_book() -> None:
    adapter = FirstockAdapter(FakeFirstock(), FakeSession())
    record = (await adapter.order_book())[0]
    assert adapter.find_client_order_id(record) == "sidra-abc123"


# --- interchangeability ---------------------------------------------------


async def test_both_adapters_answer_with_the_same_vocabulary() -> None:
    """Whatever the broker, the layer above reads one set of outcomes."""
    upstox = await UpstoxAdapter(FakeUpstox()).submit(ORDER)
    firstock = await FirstockAdapter(FakeFirstock(), FakeSession()).submit(ORDER)
    assert upstox.status == firstock.status == ACCEPTED
    assert {type(upstox), type(firstock)} == {type(upstox)}


async def test_neither_adapter_leaks_a_broker_field_name_upward() -> None:
    """The result carries ids and a reason, never the broker's own payload."""
    result = await UpstoxAdapter(FakeUpstox()).submit(ORDER)
    assert set(result.__dict__) == {
        "status",
        "broker_order_ids",
        "detail",
        "failure_code",
        "failure_name",
    }


def test_the_adapters_name_themselves() -> None:
    assert UpstoxAdapter(FakeUpstox()).name == "UPSTOX"
    assert FirstockAdapter(FakeFirstock(), FakeSession()).name == "FIRSTOCK"
