"""The Upstox order path.

Weighted the same way as the Firstock tests, because the same mistake is the
expensive one: calling a timeout a rejection produces a second live order on top
of a first that may already exist, and calling it an acceptance produces a
position nobody can cancel. The happy path gets one test; the refusals get the
rest.
"""

import httpx
import pytest

from app.services.upstox_orders import (
    MAX_TAG_LENGTH,
    UpstoxApiError,
    UpstoxAuthError,
    UpstoxError,
    UpstoxOrderClient,
    UpstoxRateLimitError,
    UpstoxReportClient,
    UpstoxSession,
    UpstoxTransportUnknown,
    order_ids_from,
)


class FakeSettings:
    upstox_rate_limit_per_second = 1000.0
    upstox_algo_name = None


def respond(status_code: int, body, *, monkeypatch):
    """Patch httpx so every request returns this response."""

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def request(self, *args, **kwargs):
            FakeAsyncClient.calls.append((args, kwargs))
            return FakeResponse()

    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def raising(exception, *, monkeypatch):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def request(self, *args, **kwargs):
            raise exception

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


def order_client() -> UpstoxOrderClient:
    return UpstoxOrderClient(FakeSettings(), UpstoxSession(access_token="secret-token"))


def report_client() -> UpstoxReportClient:
    return UpstoxReportClient(FakeSettings(), UpstoxSession(access_token="secret-token"))


PLACE = dict(
    instrument_token="NSE_EQ|INE669E01016",
    quantity=10,
    product="I",
    order_type="LIMIT",
    transaction_type="BUY",
    price=418.0,
    tag="sidra-abc123",
)


# --- the boundary ---------------------------------------------------------


@pytest.mark.parametrize("name", ["place_order", "cancel_order"])
def test_the_report_client_cannot_change_an_order(name: str) -> None:
    """What makes reconciliation and recovery safe by construction."""
    assert not hasattr(UpstoxReportClient, name)


@pytest.mark.parametrize("name", ["place_order", "cancel_order"])
def test_the_order_client_can(name: str) -> None:
    assert hasattr(UpstoxOrderClient, name)


# --- placement outcomes ---------------------------------------------------


async def test_a_successful_placement_returns_its_order_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    respond(200, {"status": "success", "data": {"order_ids": ["1644490272000"]}}, monkeypatch=monkeypatch)
    assert await order_client().place_order(**PLACE) == ["1644490272000"]


async def test_a_sliced_placement_keeps_every_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Storing one id would lose track of real exposure."""
    respond(200, {"status": "success", "data": {"order_ids": ["1", "2", "3"]}}, monkeypatch=monkeypatch)
    assert await order_client().place_order(**PLACE) == ["1", "2", "3"]


async def test_a_timeout_is_unknown_not_a_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body was sent. The order may exist."""
    raising(httpx.TimeoutException("timed out"), monkeypatch=monkeypatch)
    with pytest.raises(UpstoxTransportUnknown):
        await order_client().place_order(**PLACE)


async def test_a_transport_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    raising(httpx.ConnectError("connection reset"), monkeypatch=monkeypatch)
    with pytest.raises(UpstoxTransportUnknown):
        await order_client().place_order(**PLACE)


async def test_an_unreadable_body_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 we cannot parse is not a refusal we can act on."""
    respond(200, ValueError("not json"), monkeypatch=monkeypatch)
    with pytest.raises(UpstoxTransportUnknown):
        await order_client().place_order(**PLACE)


async def test_a_documented_error_is_a_refusal_and_keeps_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respond(
        400,
        {
            "status": "error",
            "errors": [{"errorCode": "UDAPI1026", "message": "Invalid quantity", "propertyPath": "quantity"}],
        },
        monkeypatch=monkeypatch,
    )
    with pytest.raises(UpstoxApiError) as info:
        await order_client().place_order(**PLACE)
    assert info.value.code == "UDAPI1026"
    assert info.value.property_path == "quantity"


@pytest.mark.parametrize("status_code", [401, 403])
async def test_a_rejected_token_is_an_auth_error(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """Answered before an order exists, so it is a refusal, not an unknown."""
    respond(status_code, {"status": "error", "errors": [{"message": "unauthorised"}]}, monkeypatch=monkeypatch)
    with pytest.raises(UpstoxAuthError):
        await order_client().place_order(**PLACE)


async def test_a_rate_limit_is_its_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    respond(429, {"status": "error", "errors": [{"message": "too many"}]}, monkeypatch=monkeypatch)
    with pytest.raises(UpstoxRateLimitError):
        await order_client().place_order(**PLACE)


async def test_an_unrecognised_error_shape_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser that raises on a surprise is a parser that cannot say no."""
    respond(400, {"unexpected": True}, monkeypatch=monkeypatch)
    with pytest.raises(UpstoxApiError):
        await order_client().place_order(**PLACE)


async def test_the_access_token_is_never_in_a_raised_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstox can echo a rejected value back; these messages reach the UI."""
    respond(
        400,
        {"status": "error", "errors": [{"errorCode": "UDAPI1004", "message": "bad token secret-token"}]},
        monkeypatch=monkeypatch,
    )
    with pytest.raises(UpstoxApiError) as info:
        await order_client().place_order(**PLACE)
    assert "secret-token" not in str(info.value)
    assert "[redacted]" in str(info.value)


# --- the recovery key -----------------------------------------------------


async def test_placing_without_a_tag_is_refused() -> None:
    """Optional to Upstox, mandatory here: it is how a lost response is resolved."""
    with pytest.raises(UpstoxError, match="recovery key"):
        await order_client().place_order(**{**PLACE, "tag": ""})


async def test_an_over_long_tag_is_refused() -> None:
    with pytest.raises(UpstoxError, match="characters or fewer"):
        await order_client().place_order(**{**PLACE, "tag": "x" * (MAX_TAG_LENGTH + 1)})


async def test_a_non_positive_quantity_is_refused() -> None:
    with pytest.raises(UpstoxError, match="quantity must be positive"):
        await order_client().place_order(**{**PLACE, "quantity": 0})


async def test_the_tag_and_instrument_token_reach_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instrument_token goes through untranslated, which is the point."""
    fake = respond(200, {"status": "success", "data": {"order_ids": ["1"]}}, monkeypatch=monkeypatch)
    await order_client().place_order(**PLACE)
    body = fake.calls[0][1]["json"]
    assert body["tag"] == "sidra-abc123"
    assert body["instrument_token"] == "NSE_EQ|INE669E01016"
    assert body["slice"] is False


# --- cancellation ---------------------------------------------------------


async def test_cancelling_requires_an_order_id() -> None:
    with pytest.raises(UpstoxError, match="order_id is required"):
        await order_client().cancel_order("")


async def test_a_cancellation_returns_the_broker_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    respond(200, {"status": "success", "data": {"order_id": "1644490272000"}}, monkeypatch=monkeypatch)
    assert (await order_client().cancel_order("1644490272000"))["order_id"] == "1644490272000"


# --- reports --------------------------------------------------------------


async def test_the_order_book_returns_its_records(monkeypatch: pytest.MonkeyPatch) -> None:
    respond(
        200,
        {"status": "success", "data": [{"order_id": "1", "tag": "sidra-abc123", "status": "open"}]},
        monkeypatch=monkeypatch,
    )
    book = await report_client().order_book()
    assert book[0]["tag"] == "sidra-abc123"


async def test_an_unexpected_order_book_shape_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respond(200, {"status": "success", "data": {"unexpected": "shape"}}, monkeypatch=monkeypatch)
    assert await report_client().order_book() == []


async def test_order_details_requires_an_id() -> None:
    with pytest.raises(UpstoxError, match="order_id is required"):
        await report_client().order_details("")


# --- the id parser --------------------------------------------------------


def test_the_v3_list_shape_is_read() -> None:
    assert order_ids_from({"order_ids": ["1", "2"]}) == ["1", "2"]


def test_the_older_single_shape_is_read() -> None:
    assert order_ids_from({"order_id": "1"}) == ["1"]


def test_a_repeated_id_counts_once() -> None:
    assert order_ids_from({"order_ids": ["7", "7"], "order_id": "7"}) == ["7"]


@pytest.mark.parametrize("data", [None, {}, [], "nonsense", {"order_ids": []}, {"order_id": ""}])
def test_shapes_carrying_no_id_yield_none(data: object) -> None:
    assert order_ids_from(data) == []


# --- the algo-name header -------------------------------------------------


def client_with_algo_name(name):
    settings = FakeSettings()
    settings.upstox_algo_name = name
    return UpstoxReportClient(settings, UpstoxSession(access_token="a-token"))


def test_no_algo_name_sends_no_algo_header() -> None:
    """Below ten orders a second no algo is registered, and an unregistered
    name would be rejected rather than ignored."""
    assert "X-Algo-Name" not in client_with_algo_name(None)._headers()


def test_a_blank_algo_name_is_the_same_as_none() -> None:
    """An empty environment variable is an unset one, not an empty algo."""
    assert "X-Algo-Name" not in client_with_algo_name("   ")._headers()


def test_a_configured_algo_name_travels_verbatim() -> None:
    """Upstox matches it case-sensitively against the name in My Apps."""
    headers = client_with_algo_name("  Sidra-ORB-v1  ")._headers()
    assert headers["X-Algo-Name"] == "Sidra-ORB-v1"
