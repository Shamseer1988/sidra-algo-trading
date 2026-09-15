"""Read-only Firstock V1 order/report calls.

Phase 1 of the live-execution layer. This module deliberately contains no
placeOrder, modifyOrder or cancelOrder: it only reads broker state, so every call
here is safe to retry and no code path can create or alter a live order.

The value of building it first is that reconciliation — the thing that tells you
what the broker actually did — exists before anything can submit. It also settles
the error taxonomy that submission will depend on.

Three outcomes matter, and collapsing them is how duplicate live orders happen:

    FirstockApiError        the broker answered, and the answer was a refusal
    FirstockAuthError       the session token is invalid; re-authenticate
    FirstockTransportUnknown  we cannot prove whether the request was processed

For the read calls in this module UNKNOWN is harmless — a repeated read changes
nothing. It is defined here because submission in a later phase must distinguish
it from a refusal, and that distinction has to mean the same thing everywhere.

Contracts follow the Firstock V1 documentation. Parameter casing is preserved
exactly as documented (userId, jKey, orderNumber, tradingSymbol, priceType,
transactionType, mkt_protection) because the API is case-sensitive and a silently
renamed field is the class of bug that reaches production.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.services.firstock.client import FIRSTOCK_API_BASE_URL, FirstockError, FirstockSession

# Firstock documents 10 requests/second for order placement and for all other
# endpoints. We stay under it rather than at it, so a burst of reconciliation can
# never consume the budget an emergency cancel will need in a later phase.
DEFAULT_REQUESTS_PER_SECOND = 8.0

REQUEST_TIMEOUT_SECONDS = 15.0

# Documented authentication failure names. INVALID_JKEY is a session fault, never
# an order rejection, and must not be retried with the same token.
AUTH_ERROR_NAMES = frozenset({"INVALID_JKEY", "UNAUTHORIZED"})
RATE_LIMIT_ERROR_NAMES = frozenset({"RATE_LIMIT_EXCEEDED"})


class FirstockAuthError(FirstockError):
    """Session token rejected. Re-authenticate; do not treat as an order outcome."""


class FirstockRateLimitError(FirstockError):
    """Client exceeded Firstock's documented rate limit."""


class FirstockApiError(FirstockError):
    """Firstock answered with a documented failure envelope.

    The broker was reached and refused. Carries the documented fields so callers
    can distinguish a malformed request from a genuine business rejection.
    """

    def __init__(self, message: str, *, code: str | None, name: str | None, field: str | None) -> None:
        super().__init__(message)
        self.code = code
        self.name = name
        self.field = field


class FirstockTransportUnknown(FirstockError):
    """The request may or may not have been processed.

    Raised when the outcome cannot be established: a read timeout after the body
    was sent, a dropped connection, or a response that cannot be parsed. Callers
    performing a mutating action must reconcile rather than resubmit.
    """


@dataclass
class RateLimiter:
    """Token bucket shared by one client instance.

    Firstock's limit is per second, so a bucket that refills continuously smooths
    bursts without the thundering edge a fixed window produces.
    """

    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    _tokens: float = DEFAULT_REQUESTS_PER_SECOND
    _updated_at: float = 0.0

    def __post_init__(self) -> None:
        self._tokens = self.rate_per_second
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.rate_per_second,
                    self._tokens + (now - self._updated_at) * self.rate_per_second,
                )
                self._updated_at = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate_per_second)


class FirstockReportClient:
    """Read-only Firstock report endpoints.

    Every method here is a POST that returns broker state and changes nothing, so
    a caller may retry freely. Nothing in this class can submit, modify or cancel
    an order — that boundary is the point of the module.
    """

    def __init__(
        self,
        settings: Settings,
        session: FirstockSession,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        # A caller may pass a shared limiter so that several clients respect one
        # budget; otherwise each gets its own at the configured rate.
        self._limiter = rate_limiter or RateLimiter(rate_per_second=settings.firstock_rate_limit_per_second)

    def _auth_payload(self) -> dict[str, str]:
        return {"userId": self._session.user_id, "jKey": self._session.session_token}

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        """POST one documented endpoint and return its ``data`` payload.

        Classifies the outcome rather than raising a single opaque error, because
        the caller's correct response differs for each: re-authenticate, back off,
        surface the refusal, or reconcile.
        """
        await self._limiter.acquire()
        url = f"{FIRSTOCK_API_BASE_URL}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)) as client:
                response = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            # The request body was sent; we never learned the outcome.
            raise FirstockTransportUnknown(f"{endpoint} timed out") from exc
        except httpx.HTTPError as exc:
            raise FirstockTransportUnknown(f"{endpoint} transport failure") from exc

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            # A 200 with an unparseable body cannot be assumed to mean failure.
            raise FirstockTransportUnknown(f"{endpoint} returned an unparseable response") from exc

        if body.get("status") == "success":
            return body.get("data")

        return self._raise_for_envelope(endpoint, body)

    def _redact(self, text: str) -> str:
        """Strip the session token from anything we are about to raise or log.

        Firstock can echo a rejected value back in ``error.message`` — a validation
        failure on ``jKey`` returns the token itself. Exceptions from here reach
        structlog and the operator UI, so the token is removed at the boundary
        rather than trusted not to appear.
        """
        token = self._session.session_token
        return text.replace(token, "[redacted]") if token else text

    def _raise_for_envelope(self, endpoint: str, body: dict[str, Any]) -> Any:
        """Translate a documented failure envelope into the right exception type."""
        code = str(body.get("code")) if body.get("code") is not None else None
        name = body.get("name")
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        field = error.get("field")
        detail = self._redact(str(error.get("message") or body.get("message") or "no detail provided"))

        if name in AUTH_ERROR_NAMES or code == "401":
            raise FirstockAuthError(f"{endpoint}: session token rejected")
        if name in RATE_LIMIT_ERROR_NAMES or code == "429":
            raise FirstockRateLimitError(f"{endpoint}: rate limit exceeded")
        raise FirstockApiError(f"{endpoint}: {detail}", code=code, name=name, field=field)

    async def order_book(self) -> list[dict[str, Any]]:
        """Every order known to the broker today.

        The primary reconciliation source: it is what resolves an ambiguous
        submission, and the only way to tell a broker rejection apart from a
        transport failure.
        """
        data = await self._post("orderBook", self._auth_payload())
        return data if isinstance(data, list) else []

    async def trade_book(self) -> list[dict[str, Any]]:
        """Executions. Treat as the evidence of record for fills.

        A partial fill counts against exposure even when the remainder is later
        cancelled, so this must be read before concluding a position is flat.
        """
        data = await self._post("tradeBook", self._auth_payload())
        return data if isinstance(data, list) else []

    async def position_book(self) -> list[dict[str, Any]]:
        """Net exposure per instrument, for start-of-day and periodic checks."""
        data = await self._post("positionBook", self._auth_payload())
        return data if isinstance(data, list) else []

    async def limits(self) -> dict[str, Any]:
        """Account-level RMS/margin state."""
        data = await self._post("limit", self._auth_payload())
        return data if isinstance(data, dict) else {}

    async def single_order_history(self, order_number: str) -> list[dict[str, Any]]:
        """Lifecycle of one known order.

        Only usable once an orderNumber is held. When a submission response was
        lost entirely there is no orderNumber, and order_book() is the entry point
        instead.
        """
        if not order_number:
            raise FirstockError("order_number is required")
        payload = {**self._auth_payload(), "orderNumber": order_number}
        data = await self._post("singleOrderHistory", payload)
        return data if isinstance(data, list) else []

    async def order_margin(
        self,
        *,
        exchange: str,
        product: str,
        price_type: str,
        trading_symbol: str,
        transaction_type: str,
        price: str,
        quantity: str,
    ) -> dict[str, Any]:
        """Pre-trade margin check. Creates nothing.

        A successful call is not permission to trade: the documented response can
        report insufficient balance in ``remarks`` while still returning
        ``status: success``. Callers must read that field, not just the envelope.
        """
        payload = {
            **self._auth_payload(),
            "exchange": exchange,
            "product": product,
            "priceType": price_type,
            "tradingSymbol": trading_symbol,
            "transactionType": transaction_type,
            "price": price,
            "quantity": quantity,
        }
        data = await self._post("orderMargin", payload)
        return data if isinstance(data, dict) else {}
