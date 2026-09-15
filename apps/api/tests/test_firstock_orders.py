"""Read-only Firstock report client.

The behaviour under test is the error taxonomy. Order submission in a later phase
decides whether to reconcile or resubmit based on which exception it sees, so a
refusal being misreported as a transport failure — or the reverse — is how a
duplicate live order gets created.
"""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from app.services.firstock.client import FirstockError, FirstockSession
from app.services.firstock.orders import (
    FirstockApiError,
    FirstockAuthError,
    FirstockRateLimitError,
    FirstockReportClient,
    FirstockTransportUnknown,
    RateLimiter,
)

SESSION = FirstockSession(user_id="TESTUSER", session_token="secret-jkey-value")
SETTINGS = SimpleNamespace(firstock_rate_limit_per_second=1000.0)


class FakeResponse:
    def __init__(self, payload: object, *, json_error: Exception | None = None) -> None:
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def client_returning(payload: object, *, json_error: Exception | None = None, capture: dict | None = None):
    """Build a client whose HTTP layer yields one canned response."""

    class FakeAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> FakeResponse:
            if capture is not None:
                capture["url"] = url
                capture["json"] = json
                capture["headers"] = headers
            return FakeResponse(payload, json_error=json_error)

    return FakeAsyncClient


def client_raising(exc: Exception):
    class FakeAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            raise exc

    return FakeAsyncClient


def _install(monkeypatch: pytest.MonkeyPatch, fake: type) -> FirstockReportClient:
    monkeypatch.setattr("app.services.firstock.orders.httpx.AsyncClient", fake)
    return FirstockReportClient(SETTINGS, SESSION)


async def test_order_book_returns_documented_records(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "status": "success",
        "message": "ok",
        "data": [{"orderNumber": "24091500001", "status": "OPEN", "fillShares": "0"}],
    }
    client = _install(monkeypatch, client_returning(payload))
    records = await client.order_book()
    assert records == payload["data"]


async def test_limits_returns_object_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "success", "data": {"marginused": "0", "cash": "10000"}}
    client = _install(monkeypatch, client_returning(payload))
    assert (await client.limits())["cash"] == "10000"


async def test_non_list_data_does_not_leak_into_list_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shape change upstream must yield an empty list, never a raw dict."""
    client = _install(monkeypatch, client_returning({"status": "success", "data": {"unexpected": True}}))
    assert await client.trade_book() == []


async def test_invalid_jkey_is_an_auth_fault_not_a_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "status": "failed",
        "code": "401",
        "name": "INVALID_JKEY",
        "error": {"field": "jKey", "message": "session expired"},
    }
    client = _install(monkeypatch, client_returning(payload))
    with pytest.raises(FirstockAuthError):
        await client.position_book()


async def test_rate_limit_is_its_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "failed", "code": "429", "name": "RATE_LIMIT_EXCEEDED", "error": {}}
    client = _install(monkeypatch, client_returning(payload))
    with pytest.raises(FirstockRateLimitError):
        await client.order_book()


async def test_documented_failure_carries_its_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "status": "failed",
        "code": "400",
        "name": "BAD_REQUEST",
        "error": {"field": "quantity", "message": "quantity must be positive"},
    }
    client = _install(monkeypatch, client_returning(payload))
    with pytest.raises(FirstockApiError) as excinfo:
        await client.order_book()
    assert excinfo.value.code == "400"
    assert excinfo.value.name == "BAD_REQUEST"
    assert excinfo.value.field == "quantity"
    assert "quantity must be positive" in str(excinfo.value)


async def test_timeout_is_unknown_never_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request body was sent. We do not know what the broker did with it."""
    client = _install(monkeypatch, client_raising(httpx.ReadTimeout("timed out")))
    with pytest.raises(FirstockTransportUnknown):
        await client.order_book()


async def test_connection_error_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, client_raising(httpx.ConnectError("reset")))
    with pytest.raises(FirstockTransportUnknown):
        await client.order_book()


async def test_unparseable_body_is_unknown_not_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 we cannot read does not prove the broker rejected anything."""
    client = _install(monkeypatch, client_returning(None, json_error=ValueError("not json")))
    with pytest.raises(FirstockTransportUnknown):
        await client.order_book()


async def test_session_token_is_redacted_from_broker_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firstock echoes a rejected value back; a jKey validation failure returns the token.

    These exceptions reach structlog and the operator UI, so the token is stripped
    at the boundary rather than trusted not to appear.
    """
    payload = {
        "status": "failed",
        "code": "400",
        "name": "BAD_REQUEST",
        "error": {"field": "jKey", "message": f"rejected token {SESSION.session_token}"},
    }
    client = _install(monkeypatch, client_returning(payload))
    with pytest.raises(FirstockApiError) as excinfo:
        await client.order_book()
    assert SESSION.session_token not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


async def test_auth_error_text_carries_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "status": "failed",
        "code": "401",
        "name": "INVALID_JKEY",
        "error": {"field": "jKey", "message": SESSION.session_token},
    }
    client = _install(monkeypatch, client_returning(payload))
    with pytest.raises(FirstockAuthError) as excinfo:
        await client.order_book()
    assert SESSION.session_token not in str(excinfo.value)


async def test_request_preserves_documented_field_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firstock is case-sensitive; a renamed field is a silent production failure."""
    capture: dict = {}
    payload = {"status": "success", "data": {}}
    client = _install(monkeypatch, client_returning(payload, capture=capture))
    await client.order_margin(
        exchange="NSE",
        product="C",
        price_type="LMT",
        trading_symbol="IDEA-EQ",
        transaction_type="B",
        price="418",
        quantity="1",
    )
    assert capture["url"].endswith("/orderMargin")
    assert set(capture["json"]) == {
        "userId",
        "jKey",
        "exchange",
        "product",
        "priceType",
        "tradingSymbol",
        "transactionType",
        "price",
        "quantity",
    }
    assert capture["headers"]["Content-Type"] == "application/json"


async def test_single_order_history_requires_an_order_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an orderNumber the caller must reconcile via order_book instead."""
    client = _install(monkeypatch, client_returning({"status": "success", "data": []}))
    with pytest.raises(FirstockError):
        await client.single_order_history("")


async def test_rate_limiter_throttles_to_configured_rate() -> None:
    limiter = RateLimiter(rate_per_second=20.0)
    started = time.monotonic()
    # The bucket starts full, so the first 20 are free and the next 10 must wait.
    for _ in range(30):
        await limiter.acquire()
    assert time.monotonic() - started >= 0.4


async def test_rate_limiter_is_safe_under_concurrency() -> None:
    limiter = RateLimiter(rate_per_second=50.0)
    await asyncio.gather(*(limiter.acquire() for _ in range(50)))
