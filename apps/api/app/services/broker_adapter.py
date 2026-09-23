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

Four things the adapters are responsible for, and the reasons they are here
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

**Reading broker state back.** Reconciliation and recovery are worth testing
exhaustively, and they can only be tested against one shape — so the order book
and position book are normalised here into ``BrokerOrderRecord`` and
``BrokerPositionRecord`` rather than parsed at two call sites that would then
each need a Firstock case and an Upstox case. Statuses map onto a canonical set,
and a status that no map recognises becomes ``STATUS_UNREADABLE`` rather than
falling into the gap between "open" and "terminal", where it would be ignored.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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

# Canonical order statuses. Each broker's own spelling is mapped onto these, and
# anything a map does not recognise becomes STATUS_UNREADABLE rather than being
# quietly dropped into "not open, not terminal" — a status we cannot read is a
# state we cannot reconcile, and callers have to be able to see that.
STATUS_OPEN = "OPEN"
STATUS_COMPLETE = "COMPLETE"
STATUS_CANCELLED = "CANCELLED"
STATUS_REJECTED = "REJECTED"
STATUS_UNREADABLE = "UNREADABLE"

OPEN_STATUSES = frozenset({STATUS_OPEN})
TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_CANCELLED, STATUS_REJECTED})

# Order-book keys that may carry the identifier we sent, per broker, in
# preference order. Module-level because the Firstock contract probe reports on
# them: whether the order book echoes ``remarks`` is the one dependency of live
# recovery that Firstock's documentation does not confirm, and the diagnostic
# that answers it has to check the same names this code does.
UPSTOX_CLIENT_ID_KEYS = ("tag",)
FIRSTOCK_CLIENT_ID_KEYS = ("remarks", "remark", "Remarks")


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
class BrokerOrderDescription:
    """Exactly what the broker will be asked for, resolved before it is asked.

    This exists so the write-ahead record can name the order in the broker's own
    terms. A record that stored only the canonical form would say "BUY 10 of
    NSE_EQ|INE002A01018" while the broker was asked for "B 10 RELIANCE-EQ", and
    an operator resolving an UNKNOWN at two in the afternoon would be doing the
    translation by hand, from memory, against a live position.

    ``resolved`` is false when the instrument cannot be named at this broker —
    an index, or a symbol with no verified mapping. That is a refusal the caller
    has to see before it writes anything down, not an exception thrown from
    inside a send.
    """

    resolved: bool
    exchange: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    product: str = ""
    validity: str = ""
    detail: str = ""


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


@dataclass(frozen=True)
class BrokerOrderRecord:
    """One order-book row, in terms reconciliation and recovery can use.

    Normalising here rather than at the call site is the point of the seam: the
    reconciliation logic is worth testing exhaustively, and it can only be
    tested against one shape. ``raw`` is kept so an operator-facing report can
    still show what the broker actually said.
    """

    broker_order_id: str
    client_order_id: str | None
    status: str
    symbol: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPositionRecord:
    """One position-book row.

    ``net_quantity`` is None when the broker's value could not be parsed, never
    zero. Zero means flat, which means safe; unreadable means the exposure is
    unknown, which is the opposite, and collapsing the two would turn a reason
    to stop into a reason to continue.

    ``day_pnl`` is realised plus unrealised for this position, and follows the
    same rule for the same reason: the daily stop is computed from it, and a
    position whose P&L we cannot read is a day whose P&L we cannot bound.
    """

    symbol: str
    net_quantity: Decimal | None
    day_pnl: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarginQuote:
    """What one order needs against what the account has.

    ``readable`` is separate from the comparison because "the broker would not
    tell us" and "the broker told us there is not enough" are different facts
    that happen to have the same consequence today. A caller that could only see
    the numbers would have to invent a sentinel to tell them apart.
    """

    readable: bool
    required: Decimal | None
    available: Decimal | None
    detail: str

    @property
    def affordable(self) -> bool:
        if not self.readable or self.required is None or self.available is None:
            return False
        return self.required <= self.available


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse a broker number. Unreadable is None, never a default."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _combined_pnl(raw: dict[str, Any], *, total_keys: tuple[str, ...], parts: tuple[str, ...]) -> Decimal | None:
    """A position's P&L: a single total if the broker gives one, else its parts.

    Falls back to summing rather than to zero, and to None rather than to a
    partial sum. A missing half would understate the day by exactly the amount
    nobody noticed, which on the losing side is the amount that matters.

    ``parts`` is read as "realised, then whichever unrealised spelling exists",
    so a broker documenting two names for the same field costs a lookup rather
    than a wrong number.
    """
    for key in total_keys:
        total = _decimal_or_none(raw.get(key))
        if total is not None:
            return total
    if not parts:
        return None
    realised = _decimal_or_none(raw.get(parts[0]))
    if realised is None:
        return None
    for key in parts[1:]:
        unrealised = _decimal_or_none(raw.get(key))
        if unrealised is not None:
            return realised + unrealised
    return None


class BrokerAdapter(Protocol):
    """What the execution layer is allowed to assume about a broker."""

    name: str

    async def describe(self, order: BrokerOrder) -> BrokerOrderDescription: ...

    async def submit(self, order: BrokerOrder, description: BrokerOrderDescription) -> BrokerSubmission: ...

    async def cancel(self, broker_order_id: str) -> tuple[bool, str]: ...

    async def order_book(self) -> list[dict[str, Any]]: ...

    async def positions(self) -> list[dict[str, Any]]: ...

    async def order_margin(self, order: BrokerOrder, description: BrokerOrderDescription) -> MarginQuote: ...

    async def normalised_orders(self) -> list[BrokerOrderRecord]: ...

    async def normalised_positions(self) -> list[BrokerPositionRecord]: ...

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
    _CLIENT_ID_KEYS = UPSTOX_CLIENT_ID_KEYS
    # Upstox's documented order statuses, lower-case on the wire. Every state
    # that is not finished maps to OPEN, including the two failed-request states
    # ("not cancelled", "not modified") where the original order is still live.
    # Anything absent from this map is STATUS_UNREADABLE, never silently open.
    _STATUS = {
        "open": STATUS_OPEN,
        "open pending": STATUS_OPEN,
        "validation pending": STATUS_OPEN,
        "modify pending": STATUS_OPEN,
        "modify validation pending": STATUS_OPEN,
        "modified": STATUS_OPEN,
        "not modified": STATUS_OPEN,
        "trigger pending": STATUS_OPEN,
        "cancel pending": STATUS_OPEN,
        "not cancelled": STATUS_OPEN,
        "put order req received": STATUS_OPEN,
        "after market order req received": STATUS_OPEN,
        "modify after market order req received": STATUS_OPEN,
        "complete": STATUS_COMPLETE,
        "cancelled": STATUS_CANCELLED,
        "cancelled after market order": STATUS_CANCELLED,
        "rejected": STATUS_REJECTED,
    }
    _SEGMENT = "SEC"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def describe(self, order: BrokerOrder) -> BrokerOrderDescription:
        """Resolution is a lookup in three dictionaries and cannot reach a broker.

        The instrument token is passed through unchanged, so there is no naming
        step that can fail — only an order type or product this adapter has not
        been taught, which is a refusal rather than a KeyError at send time.
        """
        token = (order.instrument_token or "").strip()
        if not token:
            return BrokerOrderDescription(False, detail="Order carries no instrument token.")
        try:
            return BrokerOrderDescription(
                True,
                # Upstox tokens are "SEGMENT|ISIN"; the segment is the venue.
                exchange=token.split("|", 1)[0] if "|" in token else "",
                symbol=token,
                side=self._SIDE[order.side],
                order_type=self._ORDER_TYPE[order.order_type],
                product=self._PRODUCT[order.product],
                validity=order.validity,
            )
        except KeyError as exc:
            return BrokerOrderDescription(False, detail=f"Unsupported order field: {exc}")

    async def submit(self, order: BrokerOrder, description: BrokerOrderDescription) -> BrokerSubmission:
        from app.services.upstox_orders import (
            UpstoxApiError,
            UpstoxAuthError,
            UpstoxTransportUnknown,
        )

        if not description.resolved:
            return BrokerSubmission(status=REJECTED, detail=description.detail, failure_name="SYMBOL_UNRESOLVED")

        try:
            order_ids = await self._client.place_order(
                instrument_token=description.symbol,
                quantity=order.quantity,
                product=description.product,
                order_type=description.order_type,
                transaction_type=description.side,
                price=float(order.price),
                trigger_price=float(order.trigger_price),
                tag=order.client_order_id,
                validity=description.validity,
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

    async def order_margin(self, order: BrokerOrder, description: BrokerOrderDescription) -> MarginQuote:
        """Two calls, because Upstox answers the two halves separately.

        ``final_margin`` rather than ``required_margin``: the former is the cost
        after margin benefit, which is what actually has to be in the account.
        """
        from app.services.upstox_orders import UpstoxError

        if not description.resolved:
            return MarginQuote(False, None, None, description.detail)
        try:
            quote = await self._client.order_margin(
                instrument_token=description.symbol,
                quantity=order.quantity,
                product=description.product,
                transaction_type=description.side,
                price=float(order.price),
            )
            funds = await self._client.funds_and_margin(self._SEGMENT)
        except UpstoxError as exc:
            return MarginQuote(False, None, None, f"Margin check failed: {exc}")

        required = _decimal_or_none(quote.get("final_margin"))
        if required is None:
            required = _decimal_or_none(quote.get("required_margin"))
        equity = funds.get(self._SEGMENT.lower()) if isinstance(funds, dict) else None
        if not isinstance(equity, dict):
            equity = funds.get("equity") if isinstance(funds, dict) else None
        available = _decimal_or_none(equity.get("available_margin")) if isinstance(equity, dict) else None

        if required is None or available is None:
            return MarginQuote(False, required, available, "Broker margin response was not readable.")
        if required > available:
            return MarginQuote(True, required, available, f"Order needs {required} against {available} available.")
        return MarginQuote(True, required, available, f"Broker margin {available} covers {required}.")

    async def normalised_orders(self) -> list[BrokerOrderRecord]:
        records = []
        for raw in await self._client.order_book():
            if not isinstance(raw, dict):
                continue
            records.append(
                BrokerOrderRecord(
                    broker_order_id=str(raw.get("order_id") or "").strip(),
                    client_order_id=self.find_client_order_id(raw),
                    status=self._STATUS.get(str(raw.get("status") or "").strip().lower(), STATUS_UNREADABLE),
                    symbol=str(raw.get("trading_symbol") or raw.get("instrument_token") or "unknown"),
                    raw=raw,
                )
            )
        return records

    async def normalised_positions(self) -> list[BrokerPositionRecord]:
        records = []
        for raw in await self._client.positions():
            if not isinstance(raw, dict):
                continue
            records.append(
                BrokerPositionRecord(
                    symbol=str(raw.get("trading_symbol") or raw.get("instrument_token") or "unknown"),
                    net_quantity=_decimal_or_none(raw.get("quantity")),
                    day_pnl=_combined_pnl(raw, total_keys=("pnl",), parts=("realised", "unrealised")),
                    raw=raw,
                )
            )
        return records

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
    _CLIENT_ID_KEYS = FIRSTOCK_CLIENT_ID_KEYS
    # Firstock's documented order statuses, upper-case on the wire.
    _STATUS = {
        "OPEN": STATUS_OPEN,
        "PENDING": STATUS_OPEN,
        "TRIGGER_PENDING": STATUS_OPEN,
        "TRIGGER PENDING": STATUS_OPEN,
        "COMPLETE": STATUS_COMPLETE,
        "FILLED": STATUS_COMPLETE,
        "CANCELED": STATUS_CANCELLED,
        "CANCELLED": STATUS_CANCELLED,
        "REJECTED": STATUS_REJECTED,
    }

    def __init__(self, client: Any, session: AsyncSession) -> None:
        self._client = client
        self._session = session

    async def describe(self, order: BrokerOrder) -> BrokerOrderDescription:
        """Resolution reads the verified symbol map, and can legitimately fail.

        It refuses rather than raising: the caller is deciding whether to send an
        order and needs an answer either way.
        """
        from app.services.live_symbols import translate_for_order

        translation = await translate_for_order(self._session, order.instrument_token)
        if not translation.resolved or not translation.trading_symbol or not translation.exchange:
            return BrokerOrderDescription(False, detail=translation.reason)
        try:
            return BrokerOrderDescription(
                True,
                exchange=translation.exchange,
                symbol=translation.trading_symbol,
                side=self._SIDE[order.side],
                order_type=self._ORDER_TYPE[order.order_type],
                product=self._PRODUCT[order.product],
                validity=order.validity,
            )
        except KeyError as exc:
            return BrokerOrderDescription(False, detail=f"Unsupported order field: {exc}")

    async def submit(self, order: BrokerOrder, description: BrokerOrderDescription) -> BrokerSubmission:
        from app.services.firstock.orders import (
            FirstockApiError,
            FirstockAuthError,
            FirstockTransportUnknown,
        )
        from app.services.live_orders import _order_numbers

        if not description.resolved:
            return BrokerSubmission(status=REJECTED, detail=description.detail, failure_name="SYMBOL_UNRESOLVED")

        try:
            data = await self._client.place_order(
                exchange=description.exchange,
                trading_symbol=description.symbol,
                product=description.product,
                price_type=description.order_type,
                transaction_type=description.side,
                retention=description.validity,
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

    async def order_margin(self, order: BrokerOrder, description: BrokerOrderDescription) -> MarginQuote:
        """One call, which answers both halves — and can refuse inside a success.

        The documented response reports insufficient balance in ``remarks`` while
        the envelope still says success, so that field is read as a refusal even
        when the numbers would pass.
        """
        from app.services.firstock.client import FirstockError

        if not description.resolved:
            return MarginQuote(False, None, None, description.detail)
        try:
            data = await self._client.order_margin(
                exchange=description.exchange,
                product=description.product,
                price_type=description.order_type,
                trading_symbol=description.symbol,
                transaction_type=description.side,
                price=str(order.price),
                quantity=str(order.quantity),
            )
        except FirstockError as exc:
            return MarginQuote(False, None, None, f"Margin check failed: {exc}")

        required = _decimal_or_none(data.get("marginOnNewOrder"))
        available = _decimal_or_none(data.get("availableMargin"))
        if required is None or available is None:
            return MarginQuote(False, required, available, "Broker margin response was not readable.")
        if required > available:
            return MarginQuote(True, required, available, f"Order needs {required} against {available} available.")

        remarks = str(data.get("remarks") or "").strip()
        if remarks and "insufficient" in remarks.lower():
            return MarginQuote(False, required, available, f"Broker reported: {remarks}")
        return MarginQuote(True, required, available, f"Broker margin {available} covers {required}.")

    async def normalised_orders(self) -> list[BrokerOrderRecord]:
        records = []
        for raw in await self._client.order_book():
            if not isinstance(raw, dict):
                continue
            records.append(
                BrokerOrderRecord(
                    broker_order_id=str(raw.get("orderNumber") or "").strip(),
                    client_order_id=self.find_client_order_id(raw),
                    status=self._STATUS.get(str(raw.get("status") or "").strip().upper(), STATUS_UNREADABLE),
                    symbol=str(raw.get("tradingSymbol") or "unknown"),
                    raw=raw,
                )
            )
        return records

    async def normalised_positions(self) -> list[BrokerPositionRecord]:
        records = []
        for raw in await self._client.position_book():
            if not isinstance(raw, dict):
                continue
            records.append(
                BrokerPositionRecord(
                    symbol=str(raw.get("tradingSymbol") or "unknown"),
                    net_quantity=_decimal_or_none(raw.get("netQuantity")),
                    # Firstock's own reference contradicts itself here: the prose
                    # names unrealizedMTOM and the sample response shows totalMTM.
                    # Both spellings are tried rather than picking the one that
                    # happened to be written down twice.
                    day_pnl=_combined_pnl(
                        raw,
                        total_keys=("totalPNL",),
                        parts=("RealizedPNL", "unrealizedMTOM", "totalMTM"),
                    ),
                    raw=raw,
                )
            )
        return records

    def find_client_order_id(self, record: dict[str, Any]) -> str | None:
        for key in self._CLIENT_ID_KEYS:
            value = record.get(key)
            if value is not None:
                return str(value).strip()
        return None
