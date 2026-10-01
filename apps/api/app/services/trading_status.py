"""One answer to "is it trading, and if not, what is stopping it".

Seven separate things can stop this system trading: the runtime mode, the
emergency stop, the scanner's control state, the paper-tracking flag, the
administrator activation, the approval mode and the selected broker. Three stop
the scanner and four stop live orders. Each had its own card, its own wording
and its own screen, and none of them said whether any of the *others* was the
reason nothing was happening.

The operator of this system is not a developer. Asked to turn off some Telegram
messages, he came one click from pressing a button labelled "Disable paper
tracking" that halts candle evaluation, signals and live orders alike -- and
that was a reasonable reading of the label. Seven controls that each describe
themselves correctly still add up to a screen nobody can act on.

So this module answers the question once, on the server, and names **one**
blocker rather than listing gates to interpret. The order below is by
dependency, not severity: a stopped scanner makes the arming state irrelevant,
so saying "not armed" while the scanner is down would send somebody to the
wrong button.

**It reports; it never decides.** Every gate keeps enforcing itself exactly
where it did before. If this module computed something wrong, the worst case is
a misleading sentence, not an order that should have been refused -- which is
the only acceptable shape for a summary of safety state.
"""

from dataclasses import dataclass
from typing import Any

# What the card says, in the order the UI should prefer.
TRADING = "TRADING"  # armed, scanning, nothing in the way
PAUSED = "PAUSED"  # deliberately not taking new entries
BLOCKED = "BLOCKED"  # something must be cleared before it can trade
STOPPED = "STOPPED"  # the scanner itself is not running
PAPER = "PAPER"  # this deployment does not place live orders at all


@dataclass(frozen=True)
class TradingStatus:
    """What one card should say.

    ``blocker`` is a stable key for tests and logs; ``headline``, ``detail`` and
    ``remedy`` are the three lines a person reads. They are separate because an
    operator needs to know what is happening, why, and what to do about it, and
    collapsing them into one string is how screens end up saying "HARD_LOCKED".
    """

    state: str
    headline: str
    detail: str
    remedy: str
    blocker: str | None = None

    @property
    def trading(self) -> bool:
        return self.state == TRADING


def _scanner_stopped(*, control_state: str, paper_tracking: bool) -> str | None:
    """Why the scanner is not evaluating, or None.

    Two switches reach the same outcome by different routes: the worker's
    control state stops data collection, the paper-tracking flag stops
    evaluation. An operator does not need that distinction to act, so the
    difference is in ``detail`` rather than in two different cards.
    """
    if (control_state or "STOPPED").upper() != "RUNNING":
        return "scanner_control"
    if not paper_tracking:
        return "paper_tracking"
    return None


def evaluate(
    *,
    application_mode: str,
    live_trading_enabled: bool,
    emergency_stop_active: bool,
    emergency_stop_reason: str | None,
    control_state: str,
    paper_tracking: bool,
    scanner_heartbeat_fresh: bool,
    live_broker: str,
    approval_mode: str,
    armed: bool,
    reconciliation_ok: bool,
    reconciliation_detail: str,
    trades_used: int,
    trades_ceiling: int,
) -> TradingStatus:
    """The single status, from every gate that can stop trading.

    Ordered by dependency. The emergency stop comes first because it overrides
    everything beneath it; the scanner next, because an un-armed system with a
    stopped scanner should send somebody to the scanner. Daily limits come last
    because reaching them is the system working, not failing.
    """
    if emergency_stop_active:
        return TradingStatus(
            STOPPED,
            "Emergency stop is active",
            emergency_stop_reason or "Someone stopped this system.",
            "Clear the emergency stop to resume. It does not close anything already open at the broker.",
            "emergency_stop",
        )

    scanner_problem = _scanner_stopped(control_state=control_state, paper_tracking=paper_tracking)
    if scanner_problem:
        return TradingStatus(
            STOPPED,
            "The scanner is not running",
            "No candles are being evaluated, so no signals and no orders can be produced."
            if scanner_problem == "scanner_control"
            else "Signal evaluation is switched off, so nothing will be found to trade.",
            "Start the scanner to resume.",
            scanner_problem,
        )

    if not scanner_heartbeat_fresh:
        return TradingStatus(
            STOPPED,
            "The scanner has stopped responding",
            "It is set to run but has not reported in. It may have crashed or lost its market data feed.",
            "Check the scanner-worker container.",
            "scanner_heartbeat",
        )

    # Not a fault. A paper deployment is a legitimate way to run this, and
    # saying "blocked" would send somebody looking for something to fix.
    if application_mode != "LIVE" or not live_trading_enabled:
        return TradingStatus(
            PAPER,
            "Paper only",
            f"This deployment is in {application_mode} mode, so no real orders can be placed.",
            "Signals, the journal and the screens all work. Nothing reaches a broker.",
            "runtime_mode",
        )

    if (live_broker or "NONE").upper() == "NONE":
        return TradingStatus(
            BLOCKED,
            "No broker selected",
            "Live orders have nowhere to go.",
            "Choose a broker in Settings, Trading controls.",
            "live_broker",
        )

    if (approval_mode or "DISABLED").upper() == "DISABLED":
        return TradingStatus(
            BLOCKED,
            "Live orders are switched off",
            "Approval mode is DISABLED, so signals are recorded but never sent to the broker.",
            "Set an approval mode in Settings, Trading controls.",
            "approval_mode",
        )

    if not armed:
        # This IS the pause. Disarming stops new entries and leaves exits,
        # the journal and the scanner running, which is exactly what "pause"
        # should mean -- so the card calls it that instead of introducing a
        # second switch with the same effect and a different name.
        return TradingStatus(
            PAUSED,
            "Live orders paused",
            "New entries are not being placed. The scanner, the journal and exit management are still running, "
            "so anything already open is still managed to its stop, target or square-off.",
            "Resume when you want new entries again. It also expires on its own each day, which is deliberate.",
            "activation",
        )

    if not reconciliation_ok:
        return TradingStatus(
            BLOCKED,
            "Reconciliation is blocked",
            reconciliation_detail or "The broker's view and ours disagree.",
            "This clears itself once the disagreement resolves. Do not override it.",
            "reconciliation",
        )

    if trades_ceiling > 0 and trades_used >= trades_ceiling:
        return TradingStatus(
            PAUSED,
            "Done for the day",
            f"{trades_used} of {trades_ceiling} trades used.",
            "This resets tomorrow. Anything still open is managed to its stop, target or square-off.",
            "trade_ceiling",
        )

    used = f"{trades_used} of {trades_ceiling} trades used" if trades_ceiling > 0 else "no trade limit set"
    return TradingStatus(
        TRADING,
        "Live and scanning",
        f"Armed, reconciled, {used}. Waiting for a signal.",
        "Pause live orders to stop new entries while keeping exits running.",
        None,
    )


def summarise(status: TradingStatus) -> dict[str, Any]:
    return {
        "state": status.state,
        "headline": status.headline,
        "detail": status.detail,
        "remedy": status.remedy,
        "blocker": status.blocker,
        "trading": status.trading,
    }
