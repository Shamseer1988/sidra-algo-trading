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


class FakeRedis:
    """Enough of Redis for the refusal throttle, including being broken."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.fail = False

    async def set(self, key, value, ex=None, nx=False):  # noqa: ANN001, ARG002
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


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
    state.redis = FakeRedis()
    monkeypatch.setattr(module, "_controls", fake_controls)
    monkeypatch.setattr(module, "_already_handled", already)
    monkeypatch.setattr(module, "inspect_live_readiness", inspect)
    monkeypatch.setattr(module, "live_order_adapter", adapter)
    monkeypatch.setattr(module, "request_live_approval", request_approval)
    monkeypatch.setattr(module, "submit_live_order", submit)

    async def protect(_session, _settings, _adapter, _submission):
        return SimpleNamespace(protected=True, detail="Stop at 2850.00", quantity=143, step="stopped")

    monkeypatch.setattr(module, "protect_after_fill", protect)
    return state


async def run(state, *, sig=None, sett=None):
    return await module.offer_live_entry(FakeSession(), sett or settings(), state.redis, sig or signal())


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


# --- the AUTOMATIC path -----------------------------------------------------


@pytest.mark.asyncio
async def test_automatic_does_not_claim_an_operator_approved(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under AUTOMATIC nobody approved, and saying otherwise would forge consent.

    authorize_live_submission adds the operator_approval gate only under
    TELEGRAM_APPROVAL, so passing operator_approved=True here would be a lie
    that no gate would catch.
    """
    captured = {}

    async def submit(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(authorized=True, reason="Authorised"), SimpleNamespace(status="SENT")

    wiring.controls = controls(approval="AUTOMATIC")
    monkeypatch.setattr(module, "submit_live_order", submit)
    await run(wiring)
    assert "operator_approved" not in captured
    assert captured["approval_mode"] == "AUTOMATIC"
    assert captured["paper_signal_id"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["REVIEW", "SCHEDULED", "automatic_typo", ""])
async def test_an_unrecognised_approval_mode_never_submits(wiring, mode: str) -> None:
    """The dispatch used to fall through: anything that was not TELEGRAM_APPROVAL
    submitted. A mode added later would have placed orders unasked."""
    wiring.controls = controls(approval=mode)
    outcome = await run(wiring)
    assert outcome.acted is False
    assert outcome.step == "approval_mode"
    assert wiring.submitted is None
    assert wiring.requested is None


@pytest.mark.asyncio
async def test_lowercase_automatic_from_a_hand_edited_row_still_submits(wiring) -> None:
    """The settings validator upper-cases, but a row written by hand may not have."""
    wiring.controls = controls(approval="automatic")
    outcome = await run(wiring)
    assert outcome.acted is True
    assert outcome.step == "submitted"


# --- telling the operator about an unattended order -------------------------
#
# Under TELEGRAM_APPROVAL the operator is asked, so they know an order exists.
# Under AUTOMATIC nobody is asked -- and the first live order placed this way
# was rejected by the broker with no message sent at all. The operator found out
# by reading container logs. Automatic means nobody is asked, not nobody is told.


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []

    async def fake_announce(_settings, _signal, decision, submission, protection=None):
        sent.append(await _render(decision, submission))

    async def _render(decision, submission):
        if not decision.authorized:
            return f"NOT SENT: {decision.reason}"
        status = getattr(submission, "status", "UNKNOWN")
        if status in {"REJECTED", "FAILED", "UNKNOWN"}:
            return f"REJECTED: {getattr(submission, 'failure_message', None) or status}"
        return f"SENT: {status}"

    monkeypatch.setattr(module, "_announce_automatic", fake_announce)
    return sent


@pytest.mark.asyncio
async def test_a_sent_order_is_announced(wiring, announced) -> None:
    wiring.controls = controls(approval="AUTOMATIC")
    await run(wiring)
    assert announced == ["SENT: SENT"]


@pytest.mark.asyncio
async def test_a_broker_rejection_is_announced(wiring, announced, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: a real order refused by Upstox, and Telegram said nothing."""

    async def submit(*_args, **_kwargs):
        return (
            SimpleNamespace(authorized=True, reason="Authorised"),
            SimpleNamespace(status="REJECTED", failure_message="Price not required", broker_order_numbers=[]),
        )

    wiring.controls = controls(approval="AUTOMATIC")
    monkeypatch.setattr(module, "submit_live_order", submit)
    await run(wiring)
    assert announced == ["REJECTED: Price not required"]


@pytest.mark.asyncio
async def test_a_gate_refusal_is_announced(wiring, announced, monkeypatch: pytest.MonkeyPatch) -> None:
    async def submit(*_args, **_kwargs):
        return SimpleNamespace(authorized=False, reason="Daily loss limit reached"), None

    wiring.controls = controls(approval="AUTOMATIC")
    monkeypatch.setattr(module, "submit_live_order", submit)
    await run(wiring)
    assert announced == ["NOT SENT: Daily loss limit reached"]


@pytest.mark.asyncio
async def test_telegram_approval_mode_is_not_announced_twice(wiring, announced) -> None:
    """That path already asks and replies; a third message would be noise."""
    wiring.controls = controls(approval="TELEGRAM_APPROVAL")
    await run(wiring)
    assert announced == []


@pytest.mark.asyncio
async def test_a_failed_announcement_never_undoes_a_placed_order(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    """The order is already at the broker; an alert failure is a reporting problem."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("telegram down")

    wiring.controls = controls(approval="AUTOMATIC")
    monkeypatch.setattr(module, "_announce_automatic", boom)
    outcome = await run(wiring)
    # offer_live_entry catches it, so the caller still learns the order was sent.
    assert outcome.step in {"submitted", "error"}


# --- telling the operator about a refusal that happened BEFORE the ask -------
#
# The gap these close: every refusal above reached Telegram only because an
# order had already been attempted. Everything earlier -- a gate that lapsed
# after arming, an instrument the broker cannot name, a broker that stopped
# answering -- returned quietly into a container log. Under TELEGRAM_APPROVAL
# that is the entire pre-ask path, so an armed operator saw the paper alert,
# then no approval buttons, and had nothing to distinguish "the system declined"
# from "the system is broken".


@pytest.fixture
def refusal_alerts(monkeypatch: pytest.MonkeyPatch):
    """Capture what _announce_refusal actually sends, exercising its real logic.

    Patched at the Telegram boundary rather than by replacing the function, so
    the announce/silence classification, the throttle and the message body are
    all under test rather than stubbed past.
    """
    sent: list[str] = []

    class FakeNotifier:
        def __init__(self, _settings) -> None:  # noqa: ANN001
            pass

        async def send_message(self, text, keyboard=None, parse_mode=None):  # noqa: ANN001, ARG002
            sent.append(text)

    async def fake_configured(_settings):
        return SimpleNamespace(telegram_is_configured=True)

    import app.services.telegram as telegram_module
    import app.services.telegram_config as telegram_config_module

    monkeypatch.setattr(telegram_module, "TelegramNotificationService", FakeNotifier)
    monkeypatch.setattr(telegram_config_module, "configured_settings", fake_configured)
    return sent


@pytest.mark.asyncio
async def test_a_lapsed_gate_under_telegram_approval_is_not_silent(wiring, refusal_alerts) -> None:
    """The regression: armed, a gate lapses, and the operator is told nothing.

    This is the exact shape of the reconciliation going stale mid-session. The
    signal is refused before request_live_approval runs, so no approval message
    is sent either -- which left the operator watching for buttons that were
    never coming.
    """
    wiring.controls = controls(approval="TELEGRAM_APPROVAL")
    wiring.readiness = readiness(False, ("broker_reconciliation",))
    outcome = await run(wiring)

    assert outcome.acted is False
    assert wiring.requested is None
    assert len(refusal_alerts) == 1
    assert "broker_reconciliation" in refusal_alerts[0]
    assert "LIVE ENTRY BLOCKED" in refusal_alerts[0]
    # It must say what to do, not only what happened.
    assert "Risk screen" in refusal_alerts[0]


@pytest.mark.asyncio
async def test_the_pre_ask_silence_is_fixed_for_automatic_too(wiring, refusal_alerts) -> None:
    """The gap was never approval-mode specific.

    _announce_automatic runs only after submit_live_order, so under AUTOMATIC a
    gate refusal was exactly as silent as it was under TELEGRAM_APPROVAL.
    """
    wiring.controls = controls(approval="AUTOMATIC")
    wiring.readiness = readiness(False, ("administrator_activation",))
    outcome = await run(wiring)

    assert outcome.step == "gates"
    assert wiring.submitted is None
    assert len(refusal_alerts) == 1
    assert "administrator_activation" in refusal_alerts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "setup"),
    [
        ("instrument", lambda state: setattr(state, "adapter", FakeAdapter(FakeDescription(resolved=False)))),
        ("broker_unavailable", None),
    ],
)
async def test_an_actionable_pre_ask_refusal_reaches_the_operator(
    wiring, refusal_alerts, monkeypatch: pytest.MonkeyPatch, step: str, setup
) -> None:
    if setup is not None:
        setup(wiring)
    else:

        async def unavailable(*_args, **_kwargs):
            raise module.BrokerNotSelectedError("Upstox token expired")

        monkeypatch.setattr(module, "live_order_adapter", unavailable)

    outcome = await run(wiring)
    assert outcome.step == step
    assert len(refusal_alerts) == 1


@pytest.mark.asyncio
async def test_an_unexpected_error_is_announced_not_just_logged(wiring, refusal_alerts, monkeypatch) -> None:
    """The path nobody writes an alert for, which is why it is announced centrally."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(module, "live_order_adapter", boom)
    outcome = await run(wiring)
    assert outcome.step == "error"
    assert "broker exploded" in refusal_alerts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "prepare"),
    [
        ("paper deployment", lambda state: None),
        ("approval disabled", lambda state: setattr(state, "controls", controls(approval="DISABLED"))),
        ("no broker chosen", lambda state: setattr(state, "controls", controls(broker="NONE"))),
        ("already handled", lambda state: setattr(state, "handled", True)),
    ],
)
async def test_a_refusal_by_configuration_stays_silent(wiring, refusal_alerts, label: str, prepare) -> None:
    """Otherwise a deployment that declines by design alerts on every signal.

    An alert the operator has no reason to act on is worse than none: it is what
    teaches them to stop reading these messages, which would reproduce the
    original silence by a different route.
    """
    prepare(wiring)
    sett = settings(mode="PAPER") if label == "paper deployment" else settings()
    outcome = await run(wiring, sett=sett)
    assert outcome.acted is False
    assert refusal_alerts == []


@pytest.mark.asyncio
async def test_the_same_reason_is_announced_once_not_once_per_signal(wiring, refusal_alerts) -> None:
    """A failing gate refuses every signal of the session; it is one message."""
    wiring.readiness = readiness(False, ("broker_reconciliation",))
    for _ in range(4):
        await run(wiring)
    assert len(refusal_alerts) == 1


@pytest.mark.asyncio
async def test_a_different_reason_is_still_announced_inside_the_window(wiring, refusal_alerts) -> None:
    """Throttled per reason, not per path: a second, different failure is news."""
    wiring.readiness = readiness(False, ("broker_reconciliation",))
    await run(wiring)
    wiring.readiness = readiness(False, ("administrator_activation",))
    await run(wiring)
    assert len(refusal_alerts) == 2


@pytest.mark.asyncio
async def test_an_unavailable_throttle_still_sends_the_alert(wiring, refusal_alerts) -> None:
    """Fails toward telling the operator. A duplicate is a nuisance; silence is the bug."""
    wiring.redis.fail = True
    wiring.readiness = readiness(False, ("broker_reconciliation",))
    await run(wiring)
    await run(wiring)
    assert len(refusal_alerts) == 2


@pytest.mark.asyncio
async def test_an_undelivered_approval_is_not_reported_as_asked(wiring, refusal_alerts, monkeypatch) -> None:
    """request_live_approval returns the row whether or not Telegram took it.

    Treating a BLOCKED approval as "requested" would tell the scanner an
    operator had been asked when nobody was, and the row would sit unanswered
    looking like an operator who ignored it.
    """

    async def blocked(_session, _settings, **_kwargs):
        return SimpleNamespace(reference_id="ref-2", status=module.BLOCKED, block_reason="Telegram alert failed: 403")

    monkeypatch.setattr(module, "request_live_approval", blocked)
    outcome = await run(wiring)
    assert outcome.acted is False
    assert outcome.step == "approval_undelivered"
    assert "403" in outcome.detail
    # Telegram just refused the keyboard message; a plain one may still land.
    assert len(refusal_alerts) == 1


@pytest.mark.asyncio
async def test_a_failed_refusal_alert_never_changes_the_outcome(
    wiring, refusal_alerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By this point the decision not to trade is already made and recorded."""

    class BrokenNotifier:
        def __init__(self, _settings) -> None:  # noqa: ANN001
            pass

        async def send_message(self, *_args, **_kwargs):
            raise RuntimeError("telegram down")

    import app.services.telegram as telegram_module

    monkeypatch.setattr(telegram_module, "TelegramNotificationService", BrokenNotifier)
    wiring.readiness = readiness(False, ("broker_reconciliation",))
    outcome = await run(wiring)
    assert outcome.step == "gates"
    assert refusal_alerts == []


@pytest.mark.asyncio
async def test_an_unreadable_telegram_row_falls_back_to_the_environment(
    wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert must survive the database being the thing that broke.

    configured_settings reads an encrypted row. On the path reporting a fault
    that may itself be a database fault, letting that read raise would lose the
    message to the very failure it describes.
    """
    sent: list[str] = []

    class FakeNotifier:
        def __init__(self, used) -> None:  # noqa: ANN001
            self.used = used

        async def send_message(self, text, keyboard=None, parse_mode=None):  # noqa: ANN001, ARG002
            sent.append(text)

    async def unreadable(_settings):
        raise RuntimeError("database unreachable")

    import app.services.telegram as telegram_module
    import app.services.telegram_config as telegram_config_module

    monkeypatch.setattr(telegram_config_module, "configured_settings", unreadable)
    monkeypatch.setattr(telegram_module, "TelegramNotificationService", FakeNotifier)

    wiring.readiness = readiness(False, ("broker_reconciliation",))
    sett = settings()
    sett.telegram_is_configured = True
    outcome = await run(wiring, sett=sett)
    assert outcome.step == "gates"
    assert len(sent) == 1


def test_every_refusal_this_module_can_return_is_classified() -> None:
    """The completeness guard.

    The original defect was not a wrong classification but an absent one: a
    refusal nobody decided about is a refusal nobody hears. Rather than trusting
    that whoever adds the next `BridgeOutcome(False, ...)` remembers to classify
    it, this reads the steps back out of the source and fails if one belongs to
    neither set.
    """
    import re
    from pathlib import Path

    source = (Path(module.__file__)).read_text()
    steps = set(re.findall(r"BridgeOutcome\(\s*False,\s*\n?\s*[\"'](\w+)[\"']", source))
    assert steps, "no refusal steps found -- the pattern this test scans for has moved"

    classified = module.ANNOUNCED_REFUSALS | module.SILENT_REFUSALS
    unclassified = steps - classified
    assert not unclassified, (
        f"These refusals are neither announced nor deliberately silent: {sorted(unclassified)}. "
        "Add each to ANNOUNCED_REFUSALS or SILENT_REFUSALS; defaulting to silence is the bug."
    )


def test_the_two_refusal_sets_do_not_overlap() -> None:
    assert not (module.ANNOUNCED_REFUSALS & module.SILENT_REFUSALS)
