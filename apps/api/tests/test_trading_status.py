"""One card, one sentence, and the single thing that is actually in the way.

Seven gates can stop this system trading. Each described itself correctly on
its own card, and together they produced a screen the operator could not act
on: asked to quieten some Telegram messages, he came one click from pressing
"Disable paper tracking", which halts candle evaluation, signals and live
orders alike.

What these tests pin is the ORDER. Naming the wrong blocker is worse than
naming none, because it sends somebody to a button that will not help -- and
the ordering is by dependency, not by severity: a stopped scanner makes the
arming state irrelevant, so "not armed" must never be shown while the scanner
is down.
"""

import pytest

from app.services import trading_status as module
from app.services.trading_status import BLOCKED, PAPER, PAUSED, STOPPED, TRADING


def status(**overrides):
    """A healthy, trading deployment, with one thing broken per test."""
    healthy = {
        "application_mode": "LIVE",
        "live_trading_enabled": True,
        "emergency_stop_active": False,
        "emergency_stop_reason": None,
        "control_state": "RUNNING",
        "paper_tracking": True,
        "scanner_heartbeat_fresh": True,
        "live_broker": "UPSTOX",
        "approval_mode": "TELEGRAM_APPROVAL",
        "armed": True,
        "reconciliation_ok": True,
        "reconciliation_detail": "",
        "trades_used": 0,
        "trades_ceiling": 2,
    }
    return module.evaluate(**{**healthy, **overrides})


def test_a_healthy_deployment_says_it_is_trading() -> None:
    result = status()
    assert result.state == TRADING
    assert result.trading is True
    assert result.blocker is None
    assert "0 of 2" in result.detail


# --- the order, which is the whole point ----------------------------------


def test_the_emergency_stop_outranks_everything_beneath_it() -> None:
    result = status(
        emergency_stop_active=True,
        emergency_stop_reason="Operator pressed it",
        control_state="STOPPED",
        armed=False,
        reconciliation_ok=False,
    )
    assert result.blocker == "emergency_stop"
    assert "Operator pressed it" in result.detail


def test_a_stopped_scanner_outranks_not_being_armed() -> None:
    """Otherwise the card sends somebody to Arm while nothing is evaluating.

    They would arm it, see nothing happen, and have learned that the button
    lies.
    """
    result = status(control_state="STOPPED", armed=False)
    assert result.state == STOPPED
    assert result.blocker == "scanner_control"


def test_either_scanner_switch_reads_as_one_problem() -> None:
    """Two switches, two routes, one thing an operator has to do about it."""
    by_control = status(control_state="STOPPED")
    by_tracking = status(paper_tracking=False)
    assert by_control.state == by_tracking.state == STOPPED
    assert by_control.headline == by_tracking.headline
    # The difference is kept, because the two are not identical underneath.
    assert by_control.blocker != by_tracking.blocker


def test_a_silent_scanner_is_distinguished_from_a_stopped_one() -> None:
    """ "Set to run but not reporting" is a different fix from "switched off"."""
    result = status(scanner_heartbeat_fresh=False)
    assert result.blocker == "scanner_heartbeat"
    assert "container" in result.remedy


def test_a_paper_deployment_is_not_reported_as_broken() -> None:
    """It is a legitimate way to run this. BLOCKED would send someone fixing."""
    result = status(application_mode="PAPER")
    assert result.state == PAPER
    assert result.trading is False
    for word in ("blocked", "error", "fail"):
        assert word not in result.headline.lower()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"live_broker": "NONE"}, "live_broker"),
        ({"approval_mode": "DISABLED"}, "approval_mode"),
        ({"armed": False}, "activation"),
        ({"reconciliation_ok": False}, "reconciliation"),
        ({"trades_used": 2}, "trade_ceiling"),
    ],
)
def test_each_gate_names_itself_when_it_is_the_only_one(overrides: dict, expected: str) -> None:
    assert status(**overrides).blocker == expected


def test_the_broker_is_named_before_the_approval_mode() -> None:
    """Setting an approval mode with no broker chosen changes nothing."""
    assert status(live_broker="NONE", approval_mode="DISABLED").blocker == "live_broker"


def test_arming_is_named_before_reconciliation() -> None:
    """Reconciliation blocking a system nobody armed is not the operator's problem yet."""
    assert status(armed=False, reconciliation_ok=False).blocker == "activation"


# --- what the words have to do --------------------------------------------


def test_disarmed_reads_as_paused_and_says_exits_still_run() -> None:
    """The fear this answers: "if I disarm, is my open position abandoned?"

    It is not -- exits are deliberately not gated by arming -- and the card has
    to say so, or the operator will leave a system armed that they wanted to
    stop.
    """
    result = status(armed=False)
    assert result.state == PAUSED
    assert "paused" in result.headline.lower()
    assert "exit" in result.detail.lower()


def test_reaching_the_daily_ceiling_is_not_an_error() -> None:
    """It is the system working. BLOCKED would read as a fault to fix."""
    result = status(trades_used=2)
    assert result.state == PAUSED
    assert "2 of 2" in result.detail


def test_the_reconciliation_card_does_not_invite_an_override() -> None:
    """It blocked correctly all week. The card must not suggest working around it."""
    result = status(reconciliation_ok=False, reconciliation_detail="Broker order X has no local record.")
    assert result.state == BLOCKED
    assert "Broker order X" in result.detail
    assert "not override" in result.remedy.lower()


def test_every_state_gives_all_three_lines() -> None:
    """A card with an empty line reads as a bug in the card."""
    cases = [
        status(),
        status(emergency_stop_active=True),
        status(control_state="STOPPED"),
        status(scanner_heartbeat_fresh=False),
        status(application_mode="PAPER"),
        status(live_broker="NONE"),
        status(approval_mode="DISABLED"),
        status(armed=False),
        status(reconciliation_ok=False),
        status(trades_used=2),
    ]
    for result in cases:
        assert result.headline and result.detail and result.remedy, result
        assert len(result.headline) < 60, f"headline too long to be a heading: {result.headline}"


def test_only_a_trading_status_claims_to_be_trading() -> None:
    """A screen saying "trading" while something blocks is the worst failure here."""
    blocked = [
        status(emergency_stop_active=True),
        status(control_state="STOPPED"),
        status(paper_tracking=False),
        status(scanner_heartbeat_fresh=False),
        status(application_mode="PAPER"),
        status(live_trading_enabled=False),
        status(live_broker="NONE"),
        status(approval_mode="DISABLED"),
        status(armed=False),
        status(reconciliation_ok=False),
        status(trades_used=2),
    ]
    for result in blocked:
        assert result.trading is False, f"{result.blocker} claimed to be trading"
        assert result.state != TRADING
