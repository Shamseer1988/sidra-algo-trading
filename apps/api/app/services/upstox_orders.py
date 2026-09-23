"""Upstox V3 order placement and V2 reports.

The live-execution layer was built for Firstock. This is the same layer for
Upstox, and it keeps the same two properties, because they are what make the
rest of the system safe rather than merely careful:

**Two clients, one boundary.** ``UpstoxReportClient`` reads and cannot submit;
``UpstoxOrderClient`` adds the three calls that change broker state. Code handed
the report client cannot place an order however it is edited, which is what lets
reconciliation and recovery be trusted by construction.

**Three outcomes, never two.** ACCEPTED, REJECTED, UNKNOWN. A request that does
not return is not a request that did not happen: collapsing UNKNOWN into
REJECTED produces duplicate live orders, and into ACCEPTED produces phantom
positions. The distinction is drawn here so it means the same thing as it does
for Firstock.

Two ways Upstox is better than Firstock for this job, both verified against the
documentation rather than assumed:

``instrument_token`` is the same identifier the scanner already uses
    ``NSE_EQ|INE669E01016`` goes straight into the order. There is no symbol
    translation, so the entire class of "traded the wrong instrument" bug that
    ``live_symbols`` exists to prevent cannot occur on this path.

``tag`` is returned by the order book
    Confirmed in the documented response field list. That makes recovery from a
    lost placement response a lookup rather than an inference — the thing that
    remains unverified on Firstock.

Contracts, from the Upstox developer documentation:

    POST   https://api-hft.upstox.com/v3/order/place     -> data.order_ids[]
    DELETE https://api-hft.upstox.com/v3/order/cancel     -> data.order_id
    GET    https://api.upstox.com/v2/order/retrieve-all   -> order records incl. tag
    GET    https://api.upstox.com/v2/portfolio/short-term-positions
    POST   https://api.upstox.com/v2/charges/margin          -> data.final_margin
    GET    https://api.upstox.com/v2/user/get-funds-and-margin -> data.equity.available_margin

Order placement is rate limited to 10 requests per second for unregistered
algos, against 50 for everything else. The limiter here runs under the tighter
of the two for every call, so a burst of reconciliation can never consume the
budget a cancel will need.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings

UPSTOX_HFT_BASE_URL = "https://api-hft.upstox.com"
UPSTOX_API_BASE_URL = "https://api.upstox.com"

# Documented: 10 per second for order placement without SEBI algo registration.
# Staying under it rather than at it leaves room for an emergency cancel.
DEFAULT_REQUESTS_PER_SECOND = 8.0

REQUEST_TIMEOUT_SECONDS = 15.0

# Upstox error codes that mean the token is the problem, not the order.
AUTH_ERROR_CODES = frozenset({"UDAPI100050", "UDAPI100072", "UDAPI100073"})

# The tag field is documented as accepting up to 40 characters.
MAX_TAG_LENGTH = 40


class UpstoxError(RuntimeError):
    """Base for every Upstox order-path failure."""


class UpstoxAuthError(UpstoxError):
    """Access token rejected. Re-authenticate; never an order outcome."""


class UpstoxRateLimitError(UpstoxError):
    """Rate limit exceeded. Back off; the order was not accepted."""


class UpstoxApiError(UpstoxError):
    """Upstox answered with a documented error, and the answer was a refusal."""

    def __init__(self, message: str, *, code: str | None = None, property_path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.property_path = property_path


class UpstoxTransportUnknown(UpstoxError):
    """We cannot prove whether the request was processed.

    The only honest classification for a timeout on a placement, and the reason
    the caller must have written its intent down before sending.
    """


class RateLimiter:
    """Token bucket. Shared across a client so one budget covers every call."""

    def __init__(self, rate_per_second: float) -> None:
        self._rate = rate_per_second
        self._allowance = rate_per_second
        self._last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._allowance = min(self._rate, self._allowance + (now - self._last_check) * self._rate)
            self._last_check = now
            if self._allowance < 1.0:
                await asyncio.sleep((1.0 - self._allowance) / self._rate)
                self._allowance = 0.0
            else:
                self._allowance -= 1.0


@dataclass(frozen=True)
class UpstoxSession:
    access_token: str


class UpstoxReportClient:
    """Read-only Upstox endpoints. Nothing here can create or alter an order."""

    def __init__(
        self,
        settings: Settings,
        session: UpstoxSession,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._limiter = rate_limiter or RateLimiter(
            rate_per_second=getattr(settings, "upstox_rate_limit_per_second", DEFAULT_REQUESTS_PER_SECOND)
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._session.access_token}",
        }

    def _redact(self, text: str) -> str:
        """Strip the bearer token from anything about to be raised or logged.

        Exceptions from here reach structlog and the operator UI. An access token
        echoed back in a validation error would otherwise travel with them.
        """
        token = self._session.access_token
        return text.replace(token, "[redacted]") if token else text

    async def _request(self, method: str, url: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        """One call, with the outcome classified rather than collapsed."""
        await self._limiter.acquire()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)) as client:
                response = await client.request(method, url, headers=self._headers(), params=params, json=json)
        except httpx.TimeoutException as exc:
            raise UpstoxTransportUnknown(f"{method} {url} timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstoxTransportUnknown(f"{method} {url} transport failure") from exc

        try:
            body = response.json()
        except ValueError as exc:
            # A response we cannot read is not a response we can call a refusal.
            raise UpstoxTransportUnknown(f"{method} {url} returned an unparseable body") from exc

        if isinstance(body, dict) and body.get("status") == "success":
            return body.get("data")
        return self._raise_for_error(method, url, response.status_code, body)

    def _raise_for_error(self, method: str, url: str, status_code: int, body: Any) -> Any:
        """Translate an error body into the right exception type.

        Parsed defensively: the documentation lists error codes without pinning
        the envelope, so every field is optional and a shape we do not recognise
        still produces a refusal rather than an exception from the parser.
        """
        errors = body.get("errors") if isinstance(body, dict) else None
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        code = first.get("errorCode") or first.get("error_code")
        message = first.get("message") or (body if isinstance(body, str) else None) or "no detail provided"
        property_path = first.get("propertyPath") or first.get("property_path")
        detail = self._redact(str(message))

        if status_code in {401, 403} or (code and code in AUTH_ERROR_CODES):
            raise UpstoxAuthError(f"{method} {url}: access token rejected")
        if status_code == 429:
            raise UpstoxRateLimitError(f"{method} {url}: rate limit exceeded")
        raise UpstoxApiError(f"{method} {url}: {detail}", code=code, property_path=property_path)

    async def order_book(self) -> list[dict[str, Any]]:
        """Every order known to Upstox today.

        The primary reconciliation source and the only way to resolve a
        submission whose response was lost, because each record carries the
        ``tag`` the placement was sent with.
        """
        data = await self._request("GET", f"{UPSTOX_API_BASE_URL}/v2/order/retrieve-all")
        return data if isinstance(data, list) else []

    async def positions(self) -> list[dict[str, Any]]:
        """Net intraday exposure per instrument."""
        data = await self._request("GET", f"{UPSTOX_API_BASE_URL}/v2/portfolio/short-term-positions")
        return data if isinstance(data, list) else []

    async def order_margin(
        self,
        *,
        instrument_token: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
    ) -> dict[str, Any]:
        """What this one order would cost in margin.

        Upstox splits the question the live risk engine asks in two: this call
        says what the order requires, ``funds_and_margin`` says what the account
        has. Firstock answers both in one response, which is why the comparison
        lives above this class rather than inside it.

        Documented response: ``data.required_margin`` and ``data.final_margin``,
        plus a per-instrument ``margins[]`` breakdown. ``final_margin`` is the
        figure after margin benefit, so it is the one that has to be affordable.
        """
        if quantity <= 0:
            raise UpstoxError("quantity must be positive")
        payload = {
            "instruments": [
                {
                    "instrument_key": instrument_token,
                    "quantity": quantity,
                    "transaction_type": transaction_type,
                    "product": product,
                    "price": price,
                }
            ]
        }
        data = await self._request("POST", f"{UPSTOX_API_BASE_URL}/v2/charges/margin", json=payload)
        return data if isinstance(data, dict) else {}

    async def funds_and_margin(self, segment: str = "SEC") -> dict[str, Any]:
        """Available margin for a segment. ``SEC`` is equity, ``COM`` commodity.

        The segment is passed explicitly rather than omitted: without it the
        response carries both segments, and a caller reading ``available_margin``
        off the wrong one would authorise an equity order against commodity
        funds.
        """
        data = await self._request(
            "GET",
            f"{UPSTOX_API_BASE_URL}/v2/user/get-funds-and-margin",
            params={"segment": segment},
        )
        return data if isinstance(data, dict) else {}

    async def order_details(self, order_id: str) -> dict[str, Any]:
        """One order's current state, once an order_id is held."""
        if not order_id:
            raise UpstoxError("order_id is required")
        data = await self._request("GET", f"{UPSTOX_API_BASE_URL}/v2/order/details", params={"order_id": order_id})
        return data if isinstance(data, dict) else {}


class UpstoxOrderClient(UpstoxReportClient):
    """The read-only client plus the calls that change broker state.

    A separate class so a caller must ask for submission capability explicitly:
    code holding an ``UpstoxReportClient`` cannot place an order however it is
    edited. Nothing here retries — every one of these can fail in a way that
    leaves the broker changed and us unsure, and the only correct response is to
    read the order book with the tag we wrote down first.
    """

    async def place_order(
        self,
        *,
        instrument_token: str,
        quantity: int,
        product: str,
        order_type: str,
        transaction_type: str,
        price: float,
        tag: str,
        validity: str = "DAY",
        trigger_price: float = 0.0,
        disclosed_quantity: int = 0,
        is_amo: bool = False,
        slice_order: bool = False,
    ) -> list[str]:
        """Place one order and return every order id it produced.

        ``tag`` is optional to Upstox and mandatory here: it is the only field
        that lets a lost response be resolved against the order book, and an
        order placed without it can only be found again by guesswork.

        Returns a list because ``slice`` splits a large order into several, and a
        caller that stored one id would lose track of real exposure.
        """
        if not tag:
            raise UpstoxError("tag must carry a client order id; it is the recovery key")
        if len(tag) > MAX_TAG_LENGTH:
            raise UpstoxError(f"tag must be {MAX_TAG_LENGTH} characters or fewer")
        if quantity <= 0:
            raise UpstoxError("quantity must be positive")

        payload = {
            "quantity": quantity,
            "product": product,
            "validity": validity,
            "price": price,
            "tag": tag,
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": disclosed_quantity,
            "trigger_price": trigger_price,
            "is_amo": is_amo,
            "slice": slice_order,
        }
        data = await self._request("POST", f"{UPSTOX_HFT_BASE_URL}/v3/order/place", json=payload)
        return order_ids_from(data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Request cancellation of one order.

        A success envelope means the request was accepted, not that the order is
        gone. Confirm the terminal state from the order book rather than from
        this return value.
        """
        if not order_id:
            raise UpstoxError("order_id is required")
        data = await self._request("DELETE", f"{UPSTOX_HFT_BASE_URL}/v3/order/cancel", params={"order_id": order_id})
        return data if isinstance(data, dict) else {}


def order_ids_from(data: Any) -> list[str]:
    """Collect order ids from a placement response.

    V3 returns ``order_ids`` as a list; the older shape returned a single
    ``order_id``. Both are read, duplicates dropped, order preserved — losing a
    sliced order's remaining ids loses real exposure.
    """
    found: list[str] = []
    if isinstance(data, dict):
        for value in data.get("order_ids") or []:
            if value is not None and str(value).strip():
                found.append(str(value).strip())
        single = data.get("order_id")
        if single is not None and str(single).strip():
            found.append(str(single).strip())

    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique
