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
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_OPEN,
    STATUS_REJECTED,
    STATUS_UNREADABLE,
    STOP_LIMIT,
    STOP_MARKET,
    UNKNOWN,
    BrokerOrder,
    FirstockAdapter,
    UpstoxAdapter,
)
from app.services.firstock.orders import FirstockApiError, FirstockTransportUnknown
from app.services.upstox_orders import UpstoxApiError, UpstoxAuthError, UpstoxTransportUnknown


async def send(adapter, order):
    """Describe then submit, as the execution path does.

    The two steps are always paired in production — the description is resolved
    before the write-ahead record and handed to the send — so testing submit
    with a hand-built description would test a call shape nothing makes.
    """
    return await adapter.submit(order, await adapter.describe(order))


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
    def __init__(
        self,
        result=None,
        raises=None,
        *,
        book=None,
        held=None,
        margin=None,
        funds=None,
        margin_raises=None,
    ) -> None:
        self._result = result if result is not None else ["1644490272000"]
        self._raises = raises
        self.calls: list[dict] = []
        self.book = book
        self.held = held
        self.margin = margin if margin is not None else {"final_margin": 836.0, "required_margin": 4180.0}
        self.funds = funds if funds is not None else {"equity": {"available_margin": 50000.0}}
        self.margin_raises = margin_raises
        self.margin_calls: list[dict] = []
        self.segments: list[str] = []

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
        return self.book if self.book is not None else [{"order_id": "1", "tag": "sidra-abc123"}]

    async def positions(self):
        if self.held is not None:
            return self.held
        return [{"instrument_token": "NSE_EQ|INE002A01018", "quantity": 10}]

    async def order_margin(self, **kwargs):
        self.margin_calls.append(kwargs)
        if self.margin_raises:
            raise self.margin_raises
        return self.margin

    async def funds_and_margin(self, segment="SEC"):
        self.segments.append(segment)
        return self.funds


class FakeFirstock:
    def __init__(self, result=None, raises=None, *, book=None, held=None, margin=None, margin_raises=None) -> None:
        self._result = result if result is not None else {"orderNumber": "24092300001"}
        self._raises = raises
        self.calls: list[dict] = []
        self.book = book
        self.held = held
        self.margin = margin if margin is not None else {"availableMargin": "50000", "marginOnNewOrder": "836"}
        self.margin_raises = margin_raises
        self.margin_calls: list[dict] = []

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result

    async def cancel_order(self, order_number):
        return {"orderNumber": order_number, "rejreason": ""}

    async def order_book(self):
        return self.book if self.book is not None else [{"orderNumber": "1", "remarks": "sidra-abc123"}]

    async def position_book(self):
        if self.held is not None:
            return self.held
        return [{"tradingSymbol": "RELIANCE-EQ", "netQuantity": "10"}]

    async def order_margin(self, **kwargs):
        self.margin_calls.append(kwargs)
        if self.margin_raises:
            raise self.margin_raises
        return self.margin


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
    result = await send(UpstoxAdapter(FakeUpstox()), ORDER)
    assert result.status == ACCEPTED
    assert result.broker_order_ids == ["1644490272000"]


async def test_upstox_passes_the_instrument_token_through_untranslated() -> None:
    """No mapping step means no wrong-instrument bug on this path."""
    client = FakeUpstox()
    await send(UpstoxAdapter(client), ORDER)
    assert client.calls[0]["instrument_token"] == "NSE_EQ|INE002A01018"


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(BUY, "BUY"), (SELL, "SELL")],
)
async def test_upstox_side_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await send(UpstoxAdapter(client), BrokerOrder(**{**ORDER.__dict__, "side": canonical}))
    assert client.calls[0]["transaction_type"] == expected


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(MARKET, "MARKET"), (LIMIT, "LIMIT"), (STOP_LIMIT, "SL"), (STOP_MARKET, "SL-M")],
)
async def test_upstox_order_type_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await send(UpstoxAdapter(client), BrokerOrder(**{**ORDER.__dict__, "order_type": canonical}))
    assert client.calls[0]["order_type"] == expected


@pytest.mark.parametrize(("canonical", "expected"), [(INTRADAY, "I"), (DELIVERY, "D")])
async def test_upstox_product_translation(canonical: str, expected: str) -> None:
    client = FakeUpstox()
    await send(UpstoxAdapter(client), BrokerOrder(**{**ORDER.__dict__, "product": canonical}))
    assert client.calls[0]["product"] == expected


async def test_upstox_carries_the_client_order_id_as_a_tag() -> None:
    client = FakeUpstox()
    await send(UpstoxAdapter(client), ORDER)
    assert client.calls[0]["tag"] == "sidra-abc123"


async def test_upstox_timeout_is_unknown() -> None:
    result = await send(UpstoxAdapter(FakeUpstox(raises=UpstoxTransportUnknown("timed out"))), ORDER)
    assert result.status == UNKNOWN


async def test_upstox_refusal_keeps_its_code() -> None:
    error = UpstoxApiError("bad quantity", code="UDAPI1026")
    result = await send(UpstoxAdapter(FakeUpstox(raises=error)), ORDER)
    assert result.status == REJECTED
    assert result.failure_code == "UDAPI1026"


async def test_upstox_auth_failure_is_a_rejection_not_an_unknown() -> None:
    result = await send(UpstoxAdapter(FakeUpstox(raises=UpstoxAuthError("token"))), ORDER)
    assert result.status == REJECTED


async def test_upstox_acceptance_without_an_id_is_unknown() -> None:
    result = await send(UpstoxAdapter(FakeUpstox(result=[])), ORDER)
    assert result.status == UNKNOWN


async def test_upstox_reads_the_client_order_id_back_from_the_book() -> None:
    adapter = UpstoxAdapter(FakeUpstox())
    record = (await adapter.order_book())[0]
    assert adapter.find_client_order_id(record) == "sidra-abc123"


# --- Firstock -------------------------------------------------------------


async def test_firstock_translates_the_symbol_before_submitting() -> None:
    client = FakeFirstock()
    result = await send(FirstockAdapter(client, FakeSession()), ORDER)
    assert result.status == ACCEPTED
    assert client.calls[0]["trading_symbol"] == "RELIANCE-EQ"
    assert client.calls[0]["exchange"] == "NSE"


async def test_firstock_refuses_an_instrument_it_cannot_name() -> None:
    """A refusal, not an exception: the caller is deciding whether to send."""
    client = FakeFirstock()
    order = BrokerOrder(**{**ORDER.__dict__, "instrument_token": "NSE_EQ|INE999Z01099"})
    result = await send(FirstockAdapter(client, FakeSession()), order)
    assert result.status == REJECTED
    assert result.failure_name == "SYMBOL_UNRESOLVED"
    assert client.calls == []


async def test_firstock_refuses_an_index() -> None:
    order = BrokerOrder(**{**ORDER.__dict__, "instrument_token": "NSE_INDEX|Nifty 50"})
    result = await send(FirstockAdapter(FakeFirstock(), FakeSession()), order)
    assert result.status == REJECTED


@pytest.mark.parametrize(("canonical", "expected"), [(BUY, "B"), (SELL, "S")])
async def test_firstock_side_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await send(FirstockAdapter(client, FakeSession()), BrokerOrder(**{**ORDER.__dict__, "side": canonical}))
    assert client.calls[0]["transaction_type"] == expected


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [(MARKET, "MKT"), (LIMIT, "LMT"), (STOP_LIMIT, "SL-LMT"), (STOP_MARKET, "SL-MKT")],
)
async def test_firstock_order_type_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await send(FirstockAdapter(client, FakeSession()), BrokerOrder(**{**ORDER.__dict__, "order_type": canonical}))
    assert client.calls[0]["price_type"] == expected


@pytest.mark.parametrize(("canonical", "expected"), [(INTRADAY, "I"), (DELIVERY, "C")])
async def test_firstock_product_translation(canonical: str, expected: str) -> None:
    client = FakeFirstock()
    await send(FirstockAdapter(client, FakeSession()), BrokerOrder(**{**ORDER.__dict__, "product": canonical}))
    assert client.calls[0]["product"] == expected


async def test_firstock_carries_the_client_order_id_as_remarks() -> None:
    client = FakeFirstock()
    await send(FirstockAdapter(client, FakeSession()), ORDER)
    assert client.calls[0]["remarks"] == "sidra-abc123"


async def test_firstock_timeout_is_unknown() -> None:
    client = FakeFirstock(raises=FirstockTransportUnknown("timed out"))
    result = await send(FirstockAdapter(client, FakeSession()), ORDER)
    assert result.status == UNKNOWN


async def test_firstock_refusal_keeps_its_code() -> None:
    error = FirstockApiError("rejected", code="400", name="BAD_REQUEST", field="price")
    result = await send(FirstockAdapter(FakeFirstock(raises=error), FakeSession()), ORDER)
    assert result.status == REJECTED
    assert result.failure_code == "400"


async def test_firstock_reads_the_client_order_id_back_from_the_book() -> None:
    adapter = FirstockAdapter(FakeFirstock(), FakeSession())
    record = (await adapter.order_book())[0]
    assert adapter.find_client_order_id(record) == "sidra-abc123"


# --- interchangeability ---------------------------------------------------


async def test_both_adapters_answer_with_the_same_vocabulary() -> None:
    """Whatever the broker, the layer above reads one set of outcomes."""
    upstox = await send(UpstoxAdapter(FakeUpstox()), ORDER)
    firstock = await send(FirstockAdapter(FakeFirstock(), FakeSession()), ORDER)
    assert upstox.status == firstock.status == ACCEPTED
    assert {type(upstox), type(firstock)} == {type(upstox)}


async def test_neither_adapter_leaks_a_broker_field_name_upward() -> None:
    """The result carries ids and a reason, never the broker's own payload."""
    result = await send(UpstoxAdapter(FakeUpstox()), ORDER)
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


# --- margin ---------------------------------------------------------------


async def describe_and_quote(adapter, order=ORDER):
    return await adapter.order_margin(order, await adapter.describe(order))


async def test_upstox_compares_the_margin_after_benefit_not_before() -> None:
    """``required_margin`` is the gross figure; ``final_margin`` is the bill."""
    client = FakeUpstox(margin={"final_margin": 836.0, "required_margin": 99999.0})
    quote = await describe_and_quote(UpstoxAdapter(client))
    assert quote.affordable is True
    assert quote.required == Decimal("836.0")


async def test_upstox_reads_availability_from_the_equity_segment() -> None:
    """Asking without a segment returns both, and equity funds are not commodity funds."""
    client = FakeUpstox()
    await describe_and_quote(UpstoxAdapter(client))
    assert client.segments == ["SEC"]


async def test_upstox_refuses_when_the_order_costs_more_than_the_account_holds() -> None:
    client = FakeUpstox(funds={"equity": {"available_margin": 100.0}})
    quote = await describe_and_quote(UpstoxAdapter(client))
    assert quote.affordable is False
    assert "against 100" in quote.detail


async def test_an_unreadable_margin_is_never_affordable() -> None:
    """A number we could not parse must not be compared as though it were zero."""
    client = FakeUpstox(margin={"final_margin": "n/a", "required_margin": None})
    quote = await describe_and_quote(UpstoxAdapter(client))
    assert quote.readable is False
    assert quote.affordable is False


async def test_a_broker_that_will_not_answer_is_never_affordable() -> None:
    client = FakeUpstox(margin_raises=UpstoxTransportUnknown("charges/margin timed out"))
    quote = await describe_and_quote(UpstoxAdapter(client))
    assert quote.readable is False
    assert quote.affordable is False


async def test_firstock_refuses_inside_a_success_envelope() -> None:
    """The documented response reports a shortfall in remarks, not in the status."""
    client = FakeFirstock(
        margin={"availableMargin": "50000", "marginOnNewOrder": "836", "remarks": "Insufficient balance"}
    )
    quote = await describe_and_quote(FirstockAdapter(client, FakeSession()))
    assert quote.affordable is False
    assert "Insufficient balance" in quote.detail


async def test_a_symbol_that_cannot_be_named_is_never_asked_about() -> None:
    """There is nothing to ask the broker about an instrument it has no name for."""
    client = FakeUpstox()
    quote = await UpstoxAdapter(client).order_margin(
        ORDER, await UpstoxAdapter(client).describe(BrokerOrder(**{**ORDER.__dict__, "instrument_token": ""}))
    )
    assert quote.readable is False
    assert client.margin_calls == []


# --- reading broker state back -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("open", STATUS_OPEN),
        ("trigger pending", STATUS_OPEN),
        ("put order req received", STATUS_OPEN),
        ("not cancelled", STATUS_OPEN),
        ("complete", STATUS_COMPLETE),
        ("cancelled", STATUS_CANCELLED),
        ("cancelled after market order", STATUS_CANCELLED),
        ("rejected", STATUS_REJECTED),
        ("COMPLETE", STATUS_COMPLETE),
        ("wednesday", STATUS_UNREADABLE),
        ("", STATUS_UNREADABLE),
    ],
)
async def test_upstox_statuses_map_onto_the_canonical_set(raw: str, expected: str) -> None:
    adapter = UpstoxAdapter(FakeUpstox(book=[{"order_id": "1", "status": raw}]))
    assert (await adapter.normalised_orders())[0].status == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OPEN", STATUS_OPEN),
        ("TRIGGER_PENDING", STATUS_OPEN),
        ("PENDING", STATUS_OPEN),
        ("COMPLETE", STATUS_COMPLETE),
        ("CANCELLED", STATUS_CANCELLED),
        ("REJECTED", STATUS_REJECTED),
        ("something else", STATUS_UNREADABLE),
    ],
)
async def test_firstock_statuses_map_onto_the_canonical_set(raw: str, expected: str) -> None:
    adapter = FirstockAdapter(FakeFirstock(book=[{"orderNumber": "1", "status": raw}]), FakeSession())
    assert (await adapter.normalised_orders())[0].status == expected


async def test_an_unrecognised_status_is_never_silently_working_or_finished() -> None:
    """The one classification that must not exist is "neither, so ignore it"."""
    adapter = UpstoxAdapter(FakeUpstox(book=[{"order_id": "1", "status": "brand new state"}]))
    record = (await adapter.normalised_orders())[0]
    assert record.status not in {STATUS_OPEN, STATUS_COMPLETE, STATUS_CANCELLED, STATUS_REJECTED}


async def test_both_adapters_normalise_a_position_to_the_same_shape() -> None:
    upstox = await UpstoxAdapter(FakeUpstox()).normalised_positions()
    firstock = await FirstockAdapter(FakeFirstock(), FakeSession()).normalised_positions()
    assert upstox[0].net_quantity == firstock[0].net_quantity == Decimal("10")


@pytest.mark.parametrize("value", [None, "", "n/a", "--"])
async def test_an_unparseable_quantity_is_unreadable_rather_than_flat(value: object) -> None:
    """Zero means flat, which means safe. Unreadable is the opposite."""
    adapter = FirstockAdapter(FakeFirstock(held=[{"tradingSymbol": "X", "netQuantity": value}]), FakeSession())
    assert (await adapter.normalised_positions())[0].net_quantity is None


async def test_a_flat_position_reads_as_zero_not_as_unreadable() -> None:
    adapter = FirstockAdapter(FakeFirstock(held=[{"tradingSymbol": "X", "netQuantity": "0"}]), FakeSession())
    assert (await adapter.normalised_positions())[0].net_quantity == Decimal("0")


async def test_a_row_that_is_not_a_record_is_skipped_rather_than_raising() -> None:
    """A broker response shape we did not expect must not crash reconciliation."""
    adapter = UpstoxAdapter(FakeUpstox(book=["nonsense", None], held=["nonsense"]))
    assert await adapter.normalised_orders() == []
    assert await adapter.normalised_positions() == []


# --- description ----------------------------------------------------------


async def test_an_order_type_no_adapter_knows_is_refused_not_raised() -> None:
    order = BrokerOrder(**{**ORDER.__dict__, "order_type": "ICEBERG"})
    description = await UpstoxAdapter(FakeUpstox()).describe(order)
    assert description.resolved is False
    assert "ICEBERG" in description.detail


async def test_the_upstox_description_names_the_venue_from_the_token() -> None:
    description = await UpstoxAdapter(FakeUpstox()).describe(ORDER)
    assert description.exchange == "NSE_EQ"
    assert description.symbol == "NSE_EQ|INE002A01018"
