"""The link between a signal and a real order, and every way it must not fire.

This module exists because its absence was invisible: the scanner worked, the
gates worked, the approval flow worked, and no order could ever be placed
because nothing joined them. So the first test here is the one that would have
caught that -- a fully armed deployment must actually ask.

The rest are refusals. A bridge that fires when it should not is worse than one
that never fires, because the second failure is at least obvious.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import live_entry_bridge as module


@dataclass
class FakeDescription:
    resolved: bool = True
    exchange: str = "NSE_EQ"
    symbol: str = "IRCTC"
    detail: str = ""


class FakeAdapter:
    name = "UPSTOX"

    def __init__(self, description: FakeDescription | None = None) -> None:
        self._description = description or FakeDescription()
        self.described: list = []

    async def describe(self, order):  # noqa: ANN001
        self.described.append(order)
        return self._description


class FakeSession:
    async def get(self, *_args, **_kwargs):
        return None

    async def scalar(self, *_args, **_kwargs):
        return None


def settings(*, mode: str = "LIVE", enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(application_mode=mode, live_trading_enabled=enabled)


def controls(
    *,
    approval: str = "TELEGRAM_APPROVAL",
    broker: str = "UPSTOX",
    order_type: str = "MARKET",
    leverage: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        execution_approval_mode=approval,
        live_broker=broker,
        live_entry_order_type=order_type,
        intraday_leverage_enabled=leverage,
    )


def signal(side: str = "SHORT") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        instrument_token="NSE_EQ|INE335Y01020",
        side=side,
        quantity=63,
        entry_price=Decimal("449.85"),
    )


def readiness(ready: bool = True, failing: tuple[str, ...] = ()) -> SimpleNamespace:
    gates = [SimpleNamespace(key=key, passed=False) for key in failing]
    gates.append(SimpleNamespace(key="runtime_mode", passed=True))
    return SimpleNamespace(overall_ready=ready, gates=gates)


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(requested=None, submitted=None, adapter=FakeAdapter(), handled=False)

    async def fake_controls(_session):
        return state.controls

    async def already(_session, _signal_id):
        return state.handled

    async def inspect(_session, _settings):
        return state.readiness

    async def adapter(_settings, _session):
        return state.adapter

    async def request_approval(_session, _settings, *, request, description, broker, paper_signal_id):
        state.requested = SimpleNamespace(request=request, broker=broker, signal=paper_signal_id)
        return SimpleNamespace(reference_id="ref-1", status="PENDING")

    async def submit(_session, _settings, _adapter, _redis, *, approval_mode, request, paper_signal_id):
        state.submitted = SimpleNamespace(request=request, mode=approval_mode, signal=paper_signal_id)
        return SimpleNamespace(authorized=True, reason="Authorised"), SimpleNamespace(status="SENT")

    state.controls = controls()
    state.readiness = readiness()
    monkeypatch.setattr(module, "_controls", fake_controls)
    monkeypatch.setattr(module, "_already_handled", already)
    monkeypatch.setattr(module, "inspect_live_readiness", inspect)
    monkeypatch.setattr(module, "live_order_adapter", adapter)
    monkeypatch.setattr(module, "request_live_approval", request_approval)
    monkeypatch.setattr(module, "submit_live_order", submit)
    return state


async def run(state, *, sig=None, sett=None):
    return await module.offer_live_entry(FakeSession(), sett or settings(), object(), sig or signal())


@pytest.mark.asyncio
async def test_an_armed_deployment_actually_asks(wiring) -> None:
    """The regression this module exists for: the link that was never made."""
    outcome = await run(wiring)
    assert outcome.acted is True
    assert outcome.step == "approval_requested"
    assert wiring.requested is not None
    assert wiring.requested.request.quantity == 63
    assert wiring.requested.request.side == "SELL"


@pytest.mark.asyncio
async def test_automatic_mode_submits_without_asking(wiring) -> None:
    wiring.controls = controls(approval="AUTOMATIC")
    outcome = await run(wiring)
    assert outcome.acted is True
    assert outcome.step == "submitted"
    assert wiring.requested is None
    assert wiring.submitted.mode == "AUTOMATIC"


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "enabled"), [("PAPER", True), ("PAPER", False), ("LIVE", False), ("REPLAY", True)])
async def test_a_runtime_not_configured_for_live_offers_nothing(wiring, mode: str, enabled: bool) -> None:
    outcome = await run(wiring, sett=settings(mode=mode, enabled=enabled))
    assert outcome.acted is False
    assert outcome.step == "runtime"
    assert wiring.requested is None and wiring.submitted is None


@pytest.mark.asyncio
async def test_disabled_approval_mode_offers_nothing(wiring) -> None:
    wiring.controls = controls(approval="DISABLED")
    outcome = await run(wiring)
    assert outcome.step == "approval_mode"
    assert wiring.requested is None and wiring.submitted is None


@pytest.mark.asyncio
async def test_no_broker_selected_offers_nothing(wiring) -> None:
    wiring.controls = controls(broker="NONE")
    outcome = await run(wiring)
    assert outcome.step == "broker"
    assert wiring.requested is None


@pytest.mark.asyncio
async def test_a_failing_gate_stops_the_signal(wiring) -> None:
    """Read at the signal, not carried from the morning: a lapsed activation stops this."""
    wiring.readiness = readiness(False, ("administrator_activation",))
    outcome = await run(wiring)
    assert outcome.acted is False
    assert outcome.step == "gates"
    assert "administrator_activation" in outcome.detail
    assert wiring.requested is None


@pytest.mark.asyncio
async def test_a_signal_already_handled_is_not_offered_twice(wiring) -> None:
    """A restarted worker or a re-delivered candle must not double an order."""
    wiring.handled = True
    outcome = await run(wiring)
    assert outcome.step == "duplicate"
    assert wiring.requested is None and wiring.submitted is None


@pytest.mark.asyncio
async def test_an_unnameable_instrument_is_refused_before_anything_is_written(wiring) -> None:
    wiring.adapter = FakeAdapter(FakeDescription(resolved=False, detail="No verified mapping"))
    outcome = await run(wiring)
    assert outcome.step == "instrument"
    assert "No verified mapping" in outcome.detail
    assert wiring.requested is None


@pytest.mark.asyncio
async def test_an_unsupported_side_is_refused(wiring) -> None:
    outcome = await run(wiring, sig=signal(side="SIDEWAYS"))
    assert outcome.step == "signal"
    assert wiring.requested is None


@pytest.mark.asyncio
async def test_a_refused_submission_is_reported_not_swallowed(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    wiring.controls = controls(approval="AUTOMATIC")

    async def refuse(*_args, **kwargs):
        return SimpleNamespace(authorized=False, reason="Daily loss limit reached"), None

    monkeypatch.setattr(module, "submit_live_order", refuse)
    outcome = await run(wiring)
    assert outcome.acted is False
    assert outcome.step == "refused"
    assert "Daily loss limit" in outcome.detail


@pytest.mark.asyncio
async def test_an_unexpected_error_never_reaches_the_scanner(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper execution must not be lost to a bug on this path."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(module, "live_order_adapter", boom)
    outcome = await run(wiring)
    assert outcome.acted is False
    assert outcome.step == "error"
    assert "broker exploded" in outcome.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured", "expected"), [("MARKET", "MARKET"), ("LIMIT", "LIMIT")])
async def test_the_entry_order_type_comes_from_settings(wiring, configured: str, expected: str) -> None:
    wiring.controls = controls(order_type=configured)
    await run(wiring)
    assert wiring.requested.request.order_type == expected


@pytest.mark.asyncio
async def test_the_entry_price_is_carried_even_for_a_market_order(wiring) -> None:
    """The margin check asks about a priced order; zero would make it meaningless."""
    wiring.controls = controls(order_type="MARKET")
    await run(wiring)
    assert wiring.requested.request.price == Decimal("449.85")


@pytest.mark.asyncio
async def test_a_long_signal_buys(wiring) -> None:
    await run(wiring, sig=signal(side="LONG"))
    assert wiring.requested.request.side == "BUY"


# --- reachability -----------------------------------------------------------
#
# The defect these guard against was not a wrong behaviour but an absent call:
# request_live_approval and submit_live_order were complete, gated and tested,
# and no application code invoked either, so an armed deployment could not place
# an order. Unit tests all passed. Nothing failed, because nothing ran.
#
# So these assert reachability rather than behaviour: every live entry point must
# have a caller outside the test suite, and the scanner must reach the bridge.


def _application_sources() -> dict[str, str]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    return {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.py")}


@pytest.mark.parametrize("entry_point", ["request_live_approval", "submit_live_order", "offer_live_entry"])
def test_every_live_entry_point_has_an_application_caller(entry_point: str) -> None:
    sources = _application_sources()
    definition = f"async def {entry_point}("
    # A module that defines the function is not a caller of it.
    callers = [name for name, text in sources.items() if f"{entry_point}(" in text and definition not in text]
    assert callers, (
        f"{entry_point} is defined but never called from application code. "
        "The live path was unreachable once already; a passing unit test does not prove a caller exists."
    )


def test_the_scanner_offers_a_live_entry_on_a_qualified_signal() -> None:
    """The specific join that was missing: signal -> live path."""
    sources = _application_sources()
    scanner = sources["services/scanner_orchestration.py"]
    assert "_offer_live_entry" in scanner
    # Called from the signal path, and after the paper alert rather than before:
    # the operator's record must complete regardless of the live outcome.
    assert scanner.index("await self._alert(signal)") < scanner.index("await self._offer_live_entry(signal)")
    assert "from app.services.live_entry_bridge import offer_live_entry" in scanner
