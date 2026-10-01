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
    def __init__(self, orders=None, positions=None, raises=None, name="UPSTOX") -> None:
        self.name = name
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

    state.asked_for = []

    async def adapter_for(_settings, _session, broker=None):
        state.asked_for.append(broker)
        if isinstance(state.adapter, Exception):
            raise state.adapter
        return state.adapter

    monkeypatch.setattr(module, "live_report_adapter", adapter_for)
    return state


async def snapshot(state, *, force=False, broker=None):
    return await module.read(object(), object(), state.redis, broker=broker, force=force)


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
    assert wiring.redis.store == {}


async def test_a_broken_cache_still_serves_the_screen(wiring) -> None:
    """Redis is an optimisation here. Losing it must not lose the page."""
    wiring.redis.fail = True
    wiring.adapter = FakeAdapter(orders=[order()], positions=[position()])
    result = await snapshot(wiring)
    assert result["readable"] is True


async def test_two_brokers_do_not_share_one_cache_entry(wiring) -> None:
    """A shared key would show one account's positions against the other.

    Six seconds is long enough for an operator to read a position that is not
    there and act on it.
    """
    wiring.adapter = FakeAdapter(positions=[position(realised="170.61")], name="UPSTOX")
    await snapshot(wiring, broker="UPSTOX")
    wiring.adapter = FakeAdapter(positions=[position(realised="-40.00")], name="FIRSTOCK")
    second = await snapshot(wiring, broker="FIRSTOCK")
    assert second["broker"] == "FIRSTOCK"
    assert second["realised"] == -40.0
    assert second["stale"] is False


async def test_a_named_broker_reaches_the_gateway(wiring) -> None:
    """Naming one is the whole point of the selector; dropping it would show
    the selected broker's books under another broker's label."""
    wiring.adapter = FakeAdapter(name="FIRSTOCK")
    await snapshot(wiring, broker="FIRSTOCK")
    assert wiring.asked_for == ["FIRSTOCK"]


async def test_no_broker_named_leaves_the_choice_to_the_gateway(wiring) -> None:
    """None means "the broker selected for trading", resolved in one place."""
    wiring.adapter = FakeAdapter()
    await snapshot(wiring)
    assert wiring.asked_for == [None]


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


# --- the selector ---------------------------------------------------------


async def test_the_selector_separates_connected_from_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different facts. Which broker trades is not which broker has a token.

    Merging them would let a screen imply that looking at Firstock's books had
    pointed live orders at Firstock.
    """
    monkeypatch.setattr(module, "selected_live_broker", _returns("UPSTOX"))
    monkeypatch.setattr("app.services.upstox_oauth.load_access_token", _returns("token"))
    result = await module.choices(object(), SimpleNamespace(firstock_is_configured=False))
    assert result["selected"] == "UPSTOX"
    assert [(item["key"], item["connected"]) for item in result["brokers"]] == [
        ("UPSTOX", True),
        ("FIRSTOCK", False),
    ]


async def test_an_unconnected_broker_is_listed_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Left out, an operator expecting two brokers cannot tell why there is one."""
    monkeypatch.setattr(module, "selected_live_broker", _returns("NONE"))
    monkeypatch.setattr("app.services.upstox_oauth.load_access_token", _returns(None))
    result = await module.choices(object(), SimpleNamespace(firstock_is_configured=False))
    assert len(result["brokers"]) == 2
    assert all(item["detail"] for item in result["brokers"])


async def test_the_selector_renders_even_with_no_trading_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A screen that throws here tells the operator less than one that says NONE."""

    async def explode(_session):
        raise RuntimeError("no settings row")

    monkeypatch.setattr(module, "selected_live_broker", explode)
    monkeypatch.setattr("app.services.upstox_oauth.load_access_token", _returns(None))
    assert (await module.choices(object(), SimpleNamespace(firstock_is_configured=True)))["selected"] == "NONE"


def _returns(value):
    async def inner(*_args, **_kwargs):
        return value

    return inner


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
