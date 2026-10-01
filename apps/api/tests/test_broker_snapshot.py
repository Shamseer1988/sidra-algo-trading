"""The broker's books, read for a screen without competing with trading.

Two things make this worth testing beyond "it returns rows".

**The rate limit is shared with order placement.** Upstox allows 2000 reads in
any thirty minutes per user, and reconciliation, margin checks and order
placement draw on the same budget. A page that fetched on every render, with a
browser polling and two tabs open, reaches that without anyone doing anything
unusual -- and the cost of arriving there is not a blank panel, it is an order
refused. So the cache is a safety property, not an optimisation.

**A broker that cannot be reached is a state, not a crash.** A screen that
receives a 500 can only say "something went wrong", which is less than the
operator already knew.
"""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import broker_snapshot as module
from app.services.broker_adapter import BrokerOrderRecord, BrokerPositionRecord


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.reads = 0
        self.fail = False

    async def get(self, key):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):  # noqa: ANN001, ARG002
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value
        return True


class FakeAdapter:
    name = "UPSTOX"

    def __init__(self, orders=None, positions=None, raises=None) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._raises = raises
        self.calls = 0

    async def normalised_orders(self):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._orders

    async def normalised_positions(self):
        if self._raises:
            raise self._raises
        return self._positions


def order(client_order_id=None, status="OPEN", symbol="TATASTEEL"):
    return BrokerOrderRecord(
        broker_order_id="2610010001",
        client_order_id=client_order_id,
        status=status,
        symbol=symbol,
        side="SELL",
        order_type="MARKET",
        quantity=67,
        filled_quantity=67,
        average_price=Decimal("183.56"),
        placed_at="2026-10-01T09:37:01Z",
    )


def position(realised="170.61", unrealised="0", net="0"):
    return BrokerPositionRecord(
        symbol="TATASTEEL",
        net_quantity=Decimal(net),
        day_pnl=Decimal(realised) + Decimal(unrealised),
        realised=Decimal(realised),
        unrealised=Decimal(unrealised),
        average_price=Decimal("183.56"),
        last_price=Decimal("180.82"),
        instrument_token="NSE_EQ|INE081A01020",
    )


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(adapter=FakeAdapter(), redis=FakeRedis())

    async def adapter_for(_settings, _session):
        if isinstance(state.adapter, Exception):
            raise state.adapter
        return state.adapter

    monkeypatch.setattr(module, "live_order_adapter", adapter_for)
    return state


async def snapshot(state, *, force=False):
    return await module.read(object(), object(), state.redis, force=force)


# --- the cache is a safety property ---------------------------------------


async def test_a_second_viewer_does_not_cost_a_second_broker_read(wiring) -> None:
    """Every render hitting the broker is how a screen costs an order."""
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    for _ in range(5):
        await snapshot(wiring)
    assert wiring.adapter.calls == 1


async def test_a_cached_payload_says_it_is_cached(wiring) -> None:
    """Freshness is reported, not implied; a stale read must look stale."""
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    first = await snapshot(wiring)
    assert first["stale"] is False
    assert (await snapshot(wiring))["stale"] is True


async def test_force_reads_the_broker_again(wiring) -> None:
    """The operator asking for fresh data is the one way past the cache."""
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    await snapshot(wiring)
    await snapshot(wiring, force=True)
    assert wiring.adapter.calls == 2


async def test_an_unreadable_result_is_never_cached(wiring) -> None:
    """Caching a failure would keep saying the broker is down after it is back."""
    wiring.adapter = FakeAdapter(raises=RuntimeError("timeout"))
    await snapshot(wiring)
    assert module.CACHE_KEY not in wiring.redis.store


async def test_a_broken_cache_still_serves_the_screen(wiring) -> None:
    """Redis is an optimisation here. Losing it must not lose the page."""
    wiring.redis.fail = True
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    result = await snapshot(wiring)
    assert result["readable"] is True


# --- an unreachable broker is a state -------------------------------------


async def test_an_unreachable_broker_reads_as_unreadable_not_an_exception(wiring) -> None:
    wiring.adapter = FakeAdapter(raises=RuntimeError("connection reset"))
    result = await snapshot(wiring)
    assert result["readable"] is False
    assert "connection reset" in result["detail"]


async def test_no_broker_selected_says_so(wiring) -> None:
    from app.services.live_execution_gateway import BrokerNotSelectedError

    wiring.adapter = BrokerNotSelectedError("live_broker is NONE")
    result = await snapshot(wiring)
    assert result["readable"] is False
    assert "NONE" in result["detail"]


# --- the numbers ----------------------------------------------------------


async def test_an_order_without_our_tag_is_marked_not_ours(wiring) -> None:
    """The question asked about every untracked order that blocked trading.

    Answered in the row, rather than by comparing two screens.
    """
    wiring.adapter = FakeAdapter(orders=[order(client_order_id="sidra-abc"), order(client_order_id=None)])
    rows = (await snapshot(wiring))["orders"]
    assert [row["ours"] for row in rows] == [True, False]


async def test_a_working_order_we_did_not_place_is_counted(wiring) -> None:
    """It is the number that explains a blocked reconciliation."""
    wiring.adapter = FakeAdapter(
        orders=[order(client_order_id="sidra-abc", status="OPEN"), order(client_order_id=None, status="OPEN")]
    )
    result = await snapshot(wiring)
    assert result["working_orders"] == 2
    assert result["untracked_working"] == 1


async def test_realised_and_unrealised_are_kept_apart(wiring) -> None:
    """Two different questions: what it cost, and what is still moving."""
    wiring.adapter = FakeAdapter(positions=[position(realised="170.61", unrealised="-12.00")])
    result = await snapshot(wiring)
    assert result["realised"] == 170.61
    assert result["unrealised"] == -12.0


async def test_a_figure_the_broker_did_not_report_stays_none(wiring) -> None:
    """None and 0.00 are different claims. Zero would be a number nobody said."""
    wiring.adapter = FakeAdapter(
        positions=[BrokerPositionRecord(symbol="X", net_quantity=Decimal("5"), realised=None, unrealised=None)]
    )
    result = await snapshot(wiring)
    assert result["positions"][0]["realised"] is None
    assert result["realised"] is None


async def test_a_flat_row_is_not_an_open_position(wiring) -> None:
    """Brokers keep squared-off rows in the book all day."""
    wiring.adapter = FakeAdapter(positions=[position(net="0"), position(net="-7")])
    assert (await snapshot(wiring))["open_positions"] == 1


async def test_the_payload_is_json_serialisable(wiring) -> None:
    """It is cached as JSON; a Decimal left in would fail only at runtime."""
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    json.dumps(await snapshot(wiring))


def test_this_router_cannot_place_or_cancel_anything() -> None:
    """A screen one misplaced tap can empty an account with is not what was asked for.

    Scanned as code, not as text: the module's own docstring explains what it
    refuses to do, and a guard that read prose would fail on the explanation
    rather than on the behaviour -- which is how the first version of this
    test failed.
    """
    import io as _io
    import tokenize
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "broker_books.py").read_text()
    code = " ".join(
        token.string
        for token in tokenize.generate_tokens(_io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    for forbidden in ("submit", "cancel", "modify", "post", "put", "delete"):
        assert forbidden not in code.lower(), f"broker_books references {forbidden!r}; this router is read-only"
    assert "get" in code.lower(), "the scan found no route at all -- it is checking nothing"
