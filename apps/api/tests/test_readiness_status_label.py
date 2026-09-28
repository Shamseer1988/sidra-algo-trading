"""HARD_LOCKED fired on a healthy system every morning, so it stopped meaning anything.

The label dates from the release whose settings validators refused to boot in
live mode, where it meant "the code will not permit this". After those were
removed it described the ordinary resting state of a correctly configured
deployment waiting to be armed. A warning that appears daily on a healthy
system trains the operator to ignore the screen it appears on.
"""

from types import SimpleNamespace

from app.services.live_readiness import _status_for


def gate(key: str, passed: bool) -> SimpleNamespace:
    return SimpleNamespace(key=key, passed=passed)


def test_everything_passing_is_ready() -> None:
    gates = [gate("runtime_mode", True), gate("administrator_activation", True)]
    assert _status_for(gates, True) == "READY"


def test_a_live_runtime_waiting_to_be_armed_is_held_not_locked() -> None:
    """The regression: the normal morning state of a live deployment."""
    gates = [gate("runtime_mode", True), gate("administrator_activation", False)]
    assert _status_for(gates, False) == "HELD"


def test_a_paper_runtime_is_reported_as_not_configured() -> None:
    """Distinct from HELD: nothing here is waiting, the deployment cannot trade."""
    gates = [gate("runtime_mode", False), gate("administrator_activation", False)]
    assert _status_for(gates, False) == "NOT_CONFIGURED"


def test_a_live_runtime_blocked_on_reconciliation_is_held() -> None:
    gates = [gate("runtime_mode", True), gate("external_reconciliation", False)]
    assert _status_for(gates, False) == "HELD"


def test_the_old_label_is_gone() -> None:
    for gates, ready in (
        ([gate("runtime_mode", True)], True),
        ([gate("runtime_mode", True), gate("administrator_activation", False)], False),
        ([gate("runtime_mode", False)], False),
    ):
        assert _status_for(gates, ready) != "HARD_LOCKED"
