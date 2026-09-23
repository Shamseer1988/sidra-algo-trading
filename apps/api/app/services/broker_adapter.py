"""One order shape, two brokers behind it.

The live-execution layer was written against Firstock and speaks Firstock's
vocabulary throughout: ``tradingSymbol``, ``priceType``, ``B`` and ``S``,
``remarks``. Upstox speaks a different one — ``instrument_token``,
``order_type``, ``BUY`` and ``SELL``, ``tag`` — and an operator is now allowed
to choose between them, so the execution path cannot keep speaking either.

This module is the seam. ``BrokerOrder`` describes an order in terms the system
already holds, and each adapter translates it on the way out. Everything above
this line — the write-ahead record, the gates, the approval flow, the recovery
lookup — stays broker-agnostic, which is what makes adding a third broker a new
file rather than an edit to the code that can place orders.

Three things the adapters are responsible for, and the reasons they are here
rather than at the call site:

**Identity.** Both brokers accept a client-chosen string on a placement and echo
it back on the order book, and it is the only thing that can resolve a
submission whose response was lost. Upstox calls it ``tag``, Firstock calls it
``remarks``, and each adapter knows which. ``find_client_order_id`` reads it back
out, so recovery never has to know which broker it is talking to.

**Instrument naming.** Upstox takes the ``instrument_token`` the scanner already
holds, unchanged. Firstock needs a translated ``tradingSymbol``, and that
translation can fail — for an index, or for a name with no verified mapping. A
failure there is a refusal, not an exception, because the caller is deciding
whether to send an order and needs an answer either way.

**Outcome classification.** Both adapters return the same three outcomes.
Collapsing UNKNOWN into REJECTED produces duplicate live orders and into
ACCEPTED produces phantom positions, so the distinction is preserved across the
seam rather than re-derived on the other side of it.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"

# Canonical vocabulary. Adapters translate outward; nothing above this line uses
# a broker's own spelling.
BUY = "BUY"
SELL = "SELL"
MARKET = "MARKET"
LIMIT = "LIMIT"
STOP_LIMIT = "SL"
STOP_MARKET = "SL-M"
INTRADAY = "INTRADAY"
DELIVERY = "DELIVERY"

BROKER_UPSTOX = "UPSTOX"
BROKER_FIRSTOCK = "FIRSTOCK"
SUPPORTED_BROKERS = frozenset({BROKER_UPSTOX, BROKER_FIRSTOCK})


@dataclass(frozen=True)
class BrokerOrder:
    """One order, in the terms this system already holds."""

    instrument_token: str
    side: str
    quantity: int
    order_type: str
    product: str
    price: Decimal
    client_order_id: str
    trigger_price: Decimal = Decimal("0")
    validity: str = "DAY"


@dataclass(frozen=True)
class BrokerSubmission:
    """What came back, classified identically whichever broker answered."""

    status: str
    broker_order_ids: list[str] = field(default_factory=list)
    detail: str = ""
    failure_code: str | None = None
    failure_name: str | None = None

    @property
    def is_unknown(self) -> bool:
        return self.status == UNKNOWN


class BrokerAdapter(Protocol):
    """What the execution layer is allowed to assume about a broker."""

    name: str

    async def submit(self, order: BrokerOrder) -> BrokerSubmission: ...

    async def cancel(self, broker_order_id: str) -> tuple[bool, str]: ...

    async def order_book(self) -> list[dict[str, Any]]: ...

    async def positions(self) -> list[dict[str, Any]]: ...

    def find_client_order_id(self, record: dict[str, Any]) -> str | None: ...


class UpstoxAdapter:
    """Upstox V3 placement.

    The simpler of the two: ``instrument_token`` passes through untranslated, so
    the instrument the scanner analysed is provably the instrument the order
    names. There is no mapping step to get wrong.
    """

    name = BROKER_UPSTOX

    _SIDE = {BUY: "BUY", SELL: "SELL"}
    _ORDER_TYPE = {MARKET: "MARKET", LIMIT: "LIMIT", STOP_LIMIT: "SL", STOP_MARKET: "SL-M"}
    _PRODUCT = {INTRADAY: "I", DELIVERY: "D"}
    # The order book echoes the placement's tag under this key.
    _CLIENT_ID_KEYS = ("tag",)

    def __init__(self, client: Any) -> None:
        self._client = client

    async def submit(self, order: BrokerOrder) -> BrokerSubmission:
        from app.services.upstox_orders import (
            UpstoxApiError,
            UpstoxAuthError,
            UpstoxTransportUnknown,
        )

        try:
            order_ids = await self._client.place_order(
                instrument_token=order.instrument_token,
                quantity=order.quantity,
                product=self._PRODUCT[order.product],
                order_type=self._ORDER_TYPE[order.order_type],
                transaction_type=self._SIDE[order.side],
                price=float(order.price),
                trigger_price=float(order.trigger_price),
                tag=order.client_order_id,
                validity=order.validity,
            )
        except UpstoxTransportUnknown as exc:
            return BrokerSubmission(status=UNKNOWN, detail=str(exc))
        except UpstoxAuthError as exc:
            # Answered before an order exists, so nothing was placed.
            return BrokerSubmission(status=REJECTED, detail=str(exc), failure_name="UNAUTHORISED")
        except UpstoxApiError as exc:
            return BrokerSubmission(status=REJECTED, detail=str(exc), failure_code=exc.code)
        except KeyError as exc:
            return BrokerSubmission(status=REJECTED, detail=f"Unsupported order field: {exc}")

        if not order_ids:
            # A success envelope with no id is an order we cannot cancel.
            return BrokerSubmission(status=UNKNOWN, detail="Broker accepted without returning an order id.")
        return BrokerSubmission(
            status=ACCEPTED, broker_order_ids=order_ids, detail=f"Accepted as {', '.join(order_ids)}"
        )

    async def cancel(self, broker_order_id: str) -> tuple[bool, str]:
        from app.services.upstox_orders import UpstoxError

        try:
            data = await self._client.cancel_order(broker_order_id)
        except UpstoxError as exc:
            return False, f"Cancellation failed: {exc}"
        if not str(data.get("order_id") or "").strip():
            return False, "Cancellation response carried no order id."
        return True, "Broker accepted the cancellation; confirm from the order book."

    async def order_book(self) -> list[dict[str, Any]]:
        return await self._client.order_book()

    async def positions(self) -> list[dict[str, Any]]:
        return await self._client.positions()

    def find_client_order_id(self, record: dict[str, Any]) -> str | None:
        for key in self._CLIENT_ID_KEYS:
            value = record.get(key)
            if value is not None:
                return str(value).strip()
        return None


class FirstockAdapter:
    """Firstock V1 placement.

    Carries the symbol translation Upstox does not need. It holds a database
    session because that translation reads the verified symbol map, and it
    refuses rather than raises when a token cannot be named at the broker: the
    caller is deciding whether to send an order and needs an answer either way.
    """

    name = BROKER_FIRSTOCK

    _SIDE = {BUY: "B", SELL: "S"}
    _ORDER_TYPE = {MARKET: "MKT", LIMIT: "LMT", STOP_LIMIT: "SL-LMT", STOP_MARKET: "SL-MKT"}
    _PRODUCT = {INTRADAY: "I", DELIVERY: "C"}
    _CLIENT_ID_KEYS = ("remarks", "remark", "Remarks")

    def __init__(self, client: Any, session: AsyncSession) -> None:
        self._client = client
        self._session = session

    async def submit(self, order: BrokerOrder) -> BrokerSubmission:
        from app.services.firstock.orders import (
            FirstockApiError,
            FirstockAuthError,
            FirstockTransportUnknown,
        )
        from app.services.live_orders import _order_numbers
        from app.services.live_symbols import translate_for_order

        translation = await translate_for_order(self._session, order.instrument_token)
        if not translation.resolved or not translation.trading_symbol or not translation.exchange:
            return BrokerSubmission(status=REJECTED, detail=translation.reason, failure_name="SYMBOL_UNRESOLVED")

        try:
            data = await self._client.place_order(
                exchange=translation.exchange,
                trading_symbol=translation.trading_symbol,
                product=self._PRODUCT[order.product],
                price_type=self._ORDER_TYPE[order.order_type],
                transaction_type=self._SIDE[order.side],
                retention=order.validity,
                quantity=str(order.quantity),
                price=str(order.price),
                trigger_price=str(order.trigger_price),
                remarks=order.client_order_id,
            )
        except FirstockTransportUnknown as exc:
            return BrokerSubmission(status=UNKNOWN, detail=str(exc))
        except FirstockAuthError as exc:
            return BrokerSubmission(status=REJECTED, detail=str(exc), failure_name="INVALID_JKEY")
        except FirstockApiError as exc:
            return BrokerSubmission(status=REJECTED, detail=str(exc), failure_code=exc.code, failure_name=exc.name)
        except KeyError as exc:
            return BrokerSubmission(status=REJECTED, detail=f"Unsupported order field: {exc}")

        numbers = _order_numbers(data)
        if not numbers:
            return BrokerSubmission(status=UNKNOWN, detail="Broker accepted without returning an order number.")
        return BrokerSubmission(status=ACCEPTED, broker_order_ids=numbers, detail=f"Accepted as {', '.join(numbers)}")

    async def cancel(self, broker_order_id: str) -> tuple[bool, str]:
        from app.services.firstock.client import FirstockError
        from app.services.firstock.orders import cancellation_confirmed

        try:
            data = await self._client.cancel_order(broker_order_id)
        except FirstockError as exc:
            return False, f"Cancellation failed: {exc}"
        return cancellation_confirmed(data)

    async def order_book(self) -> list[dict[str, Any]]:
        return await self._client.order_book()

    async def positions(self) -> list[dict[str, Any]]:
        return await self._client.position_book()

    def find_client_order_id(self, record: dict[str, Any]) -> str | None:
        for key in self._CLIENT_ID_KEYS:
            value = record.get(key)
            if value is not None:
                return str(value).strip()
        return None
