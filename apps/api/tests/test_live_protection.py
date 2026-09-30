"""Every way a position can end up unprotected, and what happens instead.

On the first live day this system opened a real short and had no way to close
it: no stop, no target, no square-off, and a reconciliation gate that then
refused every further order including an exit. These tests are the record of
what must never happen again.

The cases that matter most are not the happy one. They are: a partial fill
sizing the stop wrongly, an unreadable position getting a guessed stop, and an
UNKNOWN submission being retried into two resting stops -- each of which turns a
protective order into one that opens a position.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import live_protection as module


@dataclass
class FakeDescription:
    resolved: bool = True
    exchange: str = "NSE_EQ"
    symbol: str = "RVNL"
    detail: str = ""


class FakeAdapter:
    name = "UPSTOX"

    def __init__(self, positions, submit_results=None, description=None) -> None:
        self._positions = positions
        self._submit_results = list(submit_results or [])
        self._description = description or FakeDescription()
        self.submitted: list = []
        self.position_reads = 0

    async def normalised_positions(self):
        self.position_reads += 1
        step = self._positions[min(self.position_reads - 1, len(self._positions) - 1)]
        return step

    async def describe(self, order):  # noqa: ANN001
        return self._description

    async def submit(self, order, description):  # noqa: ANN001
        self.submitted.append(order)
        if not self._submit_results:
            return SimpleNamespace(
                status="ACCEPTED", broker_order_ids=["x1"], detail="", failure_code=None, failure_name=None
            )
        result = self._submit_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def accepted(order_id: str = "stop-1"):
    return SimpleNamespace(
        status="ACCEPTED", broker_order_ids=[order_id], detail="", failure_code=None, failure_name=None
    )


def rejected(detail: str = "margin"):
    return SimpleNamespace(status="REJECTED", broker_order_ids=[], detail=detail, failure_code="X", failure_name=None)


def unknown(detail: str = "timeout"):
    return SimpleNamespace(status="UNKNOWN", broker_order_ids=[], detail=detail, failure_code=None, failure_name=None)


def position(net, symbol: str = "RVNL", token: str | None = "NSE_EQ|INE415G01027"):
    """A real BrokerPositionRecord, not a stand-in.

    Built from the actual dataclass because the matching logic lives on it. A
    SimpleNamespace here would have passed while the live system could not join
    an order to the position it had just opened.
    """
    from app.services.broker_adapter import BrokerPositionRecord

    return [BrokerPositionRecord(symbol=symbol, net_quantity=net, day_pnl=None, instrument_token=token, raw={})]


class FakeSession:
    def __init__(self, signal) -> None:
        self._signal = signal
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self._signal

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value) -> None:
        return None


def signal(stop: str = "2850.00", token: str = "NSE_EQ|INE415G01027"):
    return SimpleNamespace(id=uuid4(), instrument_token=token, stop_price=Decimal(stop), side="SHORT")


def submission(product: str = "I", symbol: str = "RVNL", token: str = "NSE_EQ|INE415G01027"):
    return SimpleNamespace(
        client_order_id="sidra-1",
        paper_signal_id=uuid4(),
        trading_symbol=symbol,
        instrument_token=token,
        product=product,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def _fake_prepare(monkeypatch: pytest.MonkeyPatch):
    async def prepare(_session, _request, _description, **kwargs):
        return SimpleNamespace(client_order_id=kwargs["client_order_id"], status="PREPARED")

    monkeypatch.setattr(module, "prepare_submission", prepare)
    monkeypatch.setattr(module, "apply_outcome", lambda *_a, **_k: None)


async def protect(adapter, *, sig=None, sub=None):
    sig = sig or signal()
    return await module.protect_after_fill(FakeSession(sig), SimpleNamespace(), adapter, sub or submission())


# --- the position is protected ----------------------------------------------


@pytest.mark.asyncio
async def test_a_short_is_protected_by_a_buy_stop() -> None:
    adapter = FakeAdapter([position(Decimal("-143"))], [accepted()])
    outcome = await protect(adapter)
    assert outcome.protected is True
    assert outcome.step == "stopped"
    order = adapter.submitted[0]
    assert order.side == "BUY"
    assert order.quantity == 143
    assert order.order_type == "SL-M"
    assert order.trigger_price == Decimal("2850.00")


@pytest.mark.asyncio
async def test_a_long_is_protected_by_a_sell_stop() -> None:
    adapter = FakeAdapter([position(Decimal("50"))], [accepted()])
    outcome = await protect(adapter)
    assert outcome.protected is True
    assert adapter.submitted[0].side == "SELL"
    assert adapter.submitted[0].quantity == 50


@pytest.mark.asyncio
async def test_the_stop_is_sized_from_the_broker_not_the_order() -> None:
    """A partial fill sized from the order would leave the excess to OPEN a
    position in the opposite direction when the stop triggered."""
    adapter = FakeAdapter([position(Decimal("-60"))], [accepted()])
    await protect(adapter)
    assert adapter.submitted[0].quantity == 60


@pytest.mark.asyncio
async def test_the_fill_is_waited_for_rather_than_assumed() -> None:
    """A market order fills fast, but not before the next line runs."""
    adapter = FakeAdapter([position(Decimal("0")), position(Decimal("0")), position(Decimal("-143"))], [accepted()])
    outcome = await protect(adapter)
    assert outcome.protected is True
    assert adapter.position_reads >= 3
    assert adapter.submitted[0].quantity == 143


# --- nothing to protect ------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_filled_places_no_stop() -> None:
    adapter = FakeAdapter([position(Decimal("0"))])
    outcome = await protect(adapter)
    assert outcome.protected is True
    assert outcome.step == "flat"
    assert adapter.submitted == []


# --- the dangerous cases -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_position_is_escalated_not_guessed() -> None:
    """A stop for a guessed quantity can open a position rather than close one."""
    adapter = FakeAdapter([position(None)])
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert outcome.step == "unreadable"
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_an_unknown_stop_is_never_retried_or_flattened() -> None:
    """The regression that matters most.

    UNKNOWN means a stop may already be resting. A second attempt could leave
    two, and when one triggers the other opens a reversed position. Flattening
    could do the same. Only the order book settles this.
    """
    adapter = FakeAdapter([position(Decimal("-143"))], [unknown()])
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert outcome.step == "unknown_stop"
    assert len(adapter.submitted) == 1
    assert outcome.flattened is False


@pytest.mark.asyncio
async def test_a_rejected_stop_is_retried_once_then_the_position_is_closed() -> None:
    adapter = FakeAdapter([position(Decimal("-143"))], [rejected(), rejected(), accepted("close-1")])
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert outcome.step == "flattened"
    assert outcome.flattened is True
    assert len(adapter.submitted) == 3
    closing = adapter.submitted[-1]
    assert closing.order_type == "MARKET"
    assert closing.side == "BUY"
    assert closing.quantity == 143


@pytest.mark.asyncio
async def test_a_position_that_can_neither_be_stopped_nor_closed_is_escalated() -> None:
    adapter = FakeAdapter([position(Decimal("-143"))], [rejected(), rejected(), rejected("no route")])
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert outcome.step == "exposed"
    assert "by hand" in outcome.detail


@pytest.mark.asyncio
async def test_an_unnameable_instrument_does_not_silently_pass() -> None:
    adapter = FakeAdapter(
        [position(Decimal("-143"))], [accepted()], description=FakeDescription(resolved=False, detail="no mapping")
    )
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_a_raising_broker_is_treated_as_unknown_not_retried() -> None:
    adapter = FakeAdapter([position(Decimal("-143"))], [RuntimeError("connection reset")])
    outcome = await protect(adapter)
    assert outcome.protected is False
    assert outcome.step == "unknown_stop"
    assert len(adapter.submitted) == 1


@pytest.mark.asyncio
async def test_a_submission_without_a_signal_cannot_invent_a_stop_price() -> None:
    sub = submission()
    sub.paper_signal_id = None
    outcome = await module.protect_after_fill(FakeSession(None), SimpleNamespace(), FakeAdapter([]), sub)
    assert outcome.protected is False
    assert outcome.step == "no_signal"


@pytest.mark.asyncio
async def test_an_unexpected_error_never_escapes() -> None:
    class Exploding(FakeAdapter):
        async def normalised_positions(self):
            raise RuntimeError("boom")

    outcome = await protect(Exploding([]))
    assert outcome.protected is False
    assert outcome.step == "error"
    assert "boom" in outcome.detail


# --- reachability -----------------------------------------------------------
#
# The live path was unreachable once already: request_live_approval and
# submit_live_order were complete, tested and never called. The same shape of
# defect here means a position opens with no stop behind it, so the pairing is
# asserted rather than assumed.


def _service_sources() -> dict[str, str]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    return {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.py")}


def test_every_path_that_submits_an_entry_also_protects_it() -> None:
    sources = _service_sources()
    definition = "async def submit_live_order("
    submitters = {
        name: text for name, text in sources.items() if "submit_live_order(" in text and definition not in text
    }
    assert submitters, "nothing submits a live order; the entry path is unreachable again"
    for name, text in submitters.items():
        assert "protect_after_fill" in text, (
            f"{name} submits a live entry and never calls protect_after_fill. "
            "A position opened without a stop behind it is what this module exists to prevent."
        )


def test_protection_does_not_route_an_exit_through_the_entry_gates() -> None:
    """An exit must be placeable when every entry gate is refusing.

    The daily loss limit, a stale reconciliation and a lapsed activation are all
    reasons not to open a position and none is a reason to leave one open. If
    this module ever called submit_live_order, a stop would be refused exactly
    when it is needed most.
    """
    source = _service_sources()["services/live_protection.py"]
    # A call, not a mention: the docstring names it to explain why it is absent.
    assert "submit_live_order(" not in source
    assert "prepare_submission(" in source and "send_prepared_order(" in source


# --- the 30-Sep live failure ------------------------------------------------
#
# A real BHARTIARTL short filled at the broker and this returned "Nothing
# filled; there is no position to protect." No stop was placed behind a live
# position. The cause was not the fill poll being too short: it polled six
# times over nine seconds and compared the wrong two strings every time.
#
# An order is placed against an instrument token. The Upstox position book
# reports a tradable name. UpstoxAdapter.describe() puts the token in `symbol`,
# so the submission's trading_symbol was "NSE_EQ|INE397D01024" while the
# position book said "BHARTIARTL" -- and _held_quantity's symbol comparison can
# only fail by returning zero, which reads exactly like "flat".


def bharti_position(net=Decimal("-7")):
    """The position book row as Upstox actually returned it that morning."""
    from app.services.broker_adapter import BrokerPositionRecord

    return [
        BrokerPositionRecord(
            symbol="BHARTIARTL",
            net_quantity=net,
            day_pnl=Decimal("1.40"),
            instrument_token="NSE_EQ|INE397D01024",
            raw={},
        )
    ]


def bharti_submission():
    """The submission as describe() actually wrote it: token in the symbol field."""
    return submission(symbol="NSE_EQ|INE397D01024", token="NSE_EQ|INE397D01024")


@pytest.mark.asyncio
async def test_a_filled_short_is_protected_when_the_broker_names_it_differently() -> None:
    """The regression. A live position must never be read as an unfilled order."""
    adapter = FakeAdapter([bharti_position()])
    outcome = await protect(
        adapter,
        sig=signal(stop="1779.02", token="NSE_EQ|INE397D01024"),
        sub=bharti_submission(),
    )

    assert outcome.protected is True
    assert outcome.step == "stopped", f"expected a stop to be placed, got {outcome.step}: {outcome.detail}"
    assert outcome.quantity == 7
    assert adapter.submitted, "no protective order reached the broker"
    placed = adapter.submitted[0]
    # A short is closed by buying, and the stop is sized from the broker's book.
    assert placed.side == "BUY"
    assert placed.quantity == 7


@pytest.mark.asyncio
async def test_a_genuinely_unfilled_order_is_still_reported_flat() -> None:
    """The fix must not make every unfilled order look like a position."""
    adapter = FakeAdapter([[]])
    outcome = await protect(adapter, sub=bharti_submission())
    assert outcome.step == "flat"
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_a_position_in_another_instrument_is_not_mistaken_for_ours() -> None:
    """Matching must stay specific; a wrong match would stop the wrong position."""
    from app.services.broker_adapter import BrokerPositionRecord

    other = [
        BrokerPositionRecord(
            symbol="RELIANCE",
            net_quantity=Decimal("-50"),
            day_pnl=None,
            instrument_token="NSE_EQ|INE002A01018",
            raw={},
        )
    ]
    adapter = FakeAdapter([other])
    outcome = await protect(adapter, sub=bharti_submission())
    assert outcome.step == "flat"
    assert adapter.submitted == []


def test_two_unidentifiable_rows_do_not_match_each_other() -> None:
    """Empty must never equal empty, or every unknown position matches every order."""
    from app.services.broker_adapter import BrokerPositionRecord

    blank = BrokerPositionRecord(symbol="", net_quantity=Decimal("1"), instrument_token=None)
    assert blank.identifies(None, None) is False
    assert blank.identifies("", "") is False
