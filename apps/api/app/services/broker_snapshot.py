"""The broker's own view of the account, read for a screen rather than a gate.

Reconciliation already reads the order book and the position book, but it reads
them to decide whether trading is safe and throws the detail away. An operator
wants the same two books on a screen, with the money on them.

**Cached, deliberately.** Upstox allows 50 reads a second but only 2000 in any
thirty minutes, which is about 66 a minute sustained -- and a page that fetched
on every render, with a browser polling and two tabs open, reaches that without
anyone doing anything unusual. Being rate-limited is not a cosmetic failure
here: the same per-user budget is what reconciliation and order placement draw
on, so a screen left open could cost an order. One short-lived cached read,
shared by every caller, keeps a screen from competing with trading.

The TTL is short enough that the page is not misleading and long enough that
the broker is read a few times a minute however many people are looking.
Freshness is reported rather than implied, so a stale read is visible as a
stale read instead of being taken for the current state.

**It never writes.** The adapter it holds is the read-only one --
``live_report_adapter`` returns a client with no placement method on it at all,
so nothing here could place, change or withdraw an order however this file is
later edited. That is a stronger guarantee than the previous version of this
module had, which held a submission-capable adapter and was merely trusted not
to use it.

**A broker can be named.** An operator with two brokers connected wants to look
at either, not only at whichever one is currently selected for trading. Naming
one is a read: it changes nothing about where an order would go, and the
selected broker remains the only broker an order can reach.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.broker_adapter import BROKER_FIRSTOCK, BROKER_UPSTOX
from app.services.live_execution_gateway import (
    BrokerNotSelectedError,
    live_report_adapter,
    selected_live_broker,
)

logger = logging.getLogger(__name__)

# Shared by every viewer. Short enough that a position closing is seen within
# seconds, long enough that ten renders cost one broker read.
CACHE_TTL_SECONDS = 6
CACHE_KEY = "broker:snapshot"


def _cache_key(broker: str | None) -> str:
    """One cache entry per broker looked at.

    Without the suffix, looking at Firstock would serve Upstox's books to the
    next viewer for six seconds -- a position shown against the wrong account.
    """
    return f"{CACHE_KEY}:{(broker or 'SELECTED').upper()}"


def _money(value: Decimal | None) -> float | None:
    """Rupees for JSON, or None. None and 0.0 mean different things here.

    A broker that did not report a figure is not a broker reporting zero, and
    collapsing them would put a number on a screen that nobody said.
    """
    return float(value) if value is not None else None


@dataclass
class SnapshotOrder:
    broker_order_id: str
    client_order_id: str | None
    status: str
    symbol: str
    side: str | None
    order_type: str | None
    quantity: int | None
    filled_quantity: int | None
    average_price: float | None
    placed_at: str | None
    # Whether this system placed it. The question an operator asked all week
    # about every untracked order that blocked reconciliation, answered in the
    # row rather than by cross-referencing two screens.
    ours: bool


@dataclass
class SnapshotPosition:
    symbol: str
    instrument_token: str | None
    net_quantity: float | None
    average_price: float | None
    last_price: float | None
    realised: float | None
    unrealised: float | None
    day_pnl: float | None


@dataclass
class BrokerSnapshot:
    """What one broker says about the account right now."""

    broker: str
    fetched_at: str
    stale: bool
    readable: bool
    detail: str
    orders: list[SnapshotOrder] = field(default_factory=list)
    positions: list[SnapshotPosition] = field(default_factory=list)

    @property
    def realised(self) -> float | None:
        return _sum_or_none([item.realised for item in self.positions])

    @property
    def unrealised(self) -> float | None:
        return _sum_or_none([item.unrealised for item in self.positions])

    @property
    def open_positions(self) -> int:
        return sum(1 for item in self.positions if item.net_quantity not in (None, 0))

    @property
    def working_orders(self) -> int:
        return sum(1 for item in self.orders if item.status == "OPEN")

    @property
    def untracked_working(self) -> int:
        """Working orders this system did not place.

        Counted separately because it is the one number that explains a blocked
        reconciliation, and an operator should not have to read the table to
        find out whether there is one.
        """
        return sum(1 for item in self.orders if item.status == "OPEN" and not item.ours)


def _sum_or_none(values: list[float | None]) -> float | None:
    """Sum, or None when the broker reported nothing to sum.

    A total built from a list where some entries were unreported would be a
    figure presented as complete while silently missing a position.
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present), 2)


def _as_dict(snapshot: BrokerSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload.update(
        realised=snapshot.realised,
        unrealised=snapshot.unrealised,
        open_positions=snapshot.open_positions,
        working_orders=snapshot.working_orders,
        untracked_working=snapshot.untracked_working,
    )
    return payload


async def read(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    *,
    broker: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """The broker's books, from cache unless asked for a fresh read.

    Never raises. A broker that cannot be reached produces a readable=False
    snapshot with the reason in words, because a screen that throws tells an
    operator less than one that says the broker is unreachable.
    """
    key = _cache_key(broker)
    if not force:
        try:
            cached = await redis.get(key)
        except Exception:  # noqa: BLE001 - a cache miss must not fail the screen
            cached = None
        if cached:
            try:
                payload = json.loads(cached)
                payload["stale"] = True
                return payload
            except (TypeError, ValueError):
                pass

    snapshot = await _fetch(session, settings, broker)
    payload = _as_dict(snapshot)
    if snapshot.readable:
        try:
            await redis.set(key, json.dumps(payload), ex=CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001 - caching is an optimisation, not the job
            logger.warning("broker_snapshot.cache_write_failed")
    return payload


async def _fetch(session: AsyncSession, settings: Settings, broker: str | None = None) -> BrokerSnapshot:
    now = datetime.now(UTC).isoformat()
    try:
        adapter = await live_report_adapter(settings, session, broker)
    except BrokerNotSelectedError as exc:
        return BrokerSnapshot("NONE", now, False, False, f"No live broker is selected: {exc}")
    except Exception as exc:  # noqa: BLE001
        return BrokerSnapshot("NONE", now, False, False, f"Broker unavailable: {type(exc).__name__}: {exc}")

    try:
        orders = await adapter.normalised_orders()
        positions = await adapter.normalised_positions()
    except Exception as exc:  # noqa: BLE001 - an unreachable broker is a state, not a crash
        logger.warning("broker_snapshot.read_failed broker=%s error=%s", adapter.name, exc)
        return BrokerSnapshot(adapter.name, now, False, False, f"{adapter.name} could not be read: {exc}")

    return BrokerSnapshot(
        broker=adapter.name,
        fetched_at=now,
        stale=False,
        readable=True,
        detail=f"{len(orders)} order(s), {len(positions)} position row(s) at {adapter.name}.",
        orders=[
            SnapshotOrder(
                broker_order_id=item.broker_order_id,
                client_order_id=item.client_order_id,
                status=item.status,
                symbol=item.symbol,
                side=item.side,
                order_type=item.order_type,
                quantity=item.quantity,
                filled_quantity=item.filled_quantity,
                average_price=_money(item.average_price),
                placed_at=item.placed_at,
                # Our client order ids are the tag carried to the broker. An
                # order without one is one we did not place -- by hand, or by
                # something else using this account.
                ours=bool(item.client_order_id),
            )
            for item in orders
        ],
        positions=[
            SnapshotPosition(
                symbol=item.symbol,
                instrument_token=item.instrument_token,
                net_quantity=_money(item.net_quantity),
                average_price=_money(item.average_price),
                last_price=_money(item.last_price),
                realised=_money(item.realised),
                unrealised=_money(item.unrealised),
                day_pnl=_money(item.day_pnl),
            )
            for item in positions
        ],
    )


async def choices(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Which brokers can be looked at, and which one trades.

    Two separate facts, deliberately not merged. ``selected`` is the broker an
    order would go to -- the operator's choice in trading controls, and the only
    thing that decides where a live order lands. ``connected`` is merely whether
    this system holds credentials for a broker, which is what decides whether
    its books can be shown.

    Offering a broker nobody has connected would be a selector whose every
    choice ends in the same error message, so an unconnected broker is listed
    with the reason it cannot be read rather than left out: an operator who
    expected two brokers and sees one needs to know why.

    Nothing here contacts a broker. Credentials are checked locally, so opening
    the selector costs no part of the rate-limit budget that trading draws on.
    """
    try:
        selected = await selected_live_broker(session)
    except Exception:  # noqa: BLE001 - a screen must still render
        selected = "NONE"

    upstox_token = False
    upstox_detail = "No stored access token. Authorise Upstox in the Upstox console."
    try:
        from app.services.upstox_oauth import load_access_token

        upstox_token = bool(await load_access_token(settings))
    except Exception as exc:  # noqa: BLE001
        upstox_detail = f"Upstox credentials unavailable: {exc}"
    if upstox_token:
        upstox_detail = "Authorised."

    firstock_ready = bool(settings.firstock_is_configured)
    return {
        "selected": selected,
        "brokers": [
            {
                "key": BROKER_UPSTOX,
                "label": "Upstox",
                "connected": upstox_token,
                "detail": upstox_detail,
            },
            {
                "key": BROKER_FIRSTOCK,
                "label": "Firstock",
                "connected": firstock_ready,
                "detail": "Credentials configured." if firstock_ready else "Firstock credentials are not configured.",
            },
        ],
    }
