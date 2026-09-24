"""How a trade is left, per strategy, instead of one rule hard-coded for all of them.

Entry logic has been configurable for a while; the exit was not. Every strategy
left a trade the same way — a stop at the widest of structure, an ATR multiple
and a percent floor, a target at the account's minimum reward:risk, no trailing,
and no time limit at all. That last one is the gap worth naming: nothing here
ever closed a position because the day was ending. A paper position opened at
14:55 stayed open, and a live one would be squared off by the broker at its own
time and its own price, which the journal would then not match.

**Every default in this module reproduces the previous behaviour exactly.** A
test asserts it. That is the whole contract: turning this on changes nothing
until somebody chooses a different rule, so a stored strategy written before
these fields existed keeps trading the way it was measured.

**The rules travel with the signal, not with the strategy.** They are copied
into ``strategy_snapshot`` when the signal is created, and execution reads them
from there. Editing a strategy at 11:00 must not move the stop of a position
opened at 10:30 — the trade was taken under the old rules and has to be managed
and judged under them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.services.trading_calendar import MARKET_TIMEZONE

# Stop placement. One rule today, named rather than implied so the screen can
# say what it is and a second rule can be added without a migration.
WIDEST_OF = "WIDEST_OF_STRUCTURE_ATR_PERCENT"

# Target placement.
RR_MULTIPLE = "RR_MULTIPLE"
ATR_MULTIPLE = "ATR_MULTIPLE"

# Stop management after entry.
NO_TRAIL = "NONE"
BREAKEVEN_AT_R = "BREAKEVEN_AT_R"
ATR_TRAIL = "ATR_TRAIL"

STOP_RULE_LABELS = {
    WIDEST_OF: "Widest of structure, ATR multiple and a percent floor",
}
TARGET_RULE_LABELS = {
    RR_MULTIPLE: "A multiple of the risk taken",
    ATR_MULTIPLE: "A multiple of ATR from entry",
}
TRAILING_LABELS = {
    NO_TRAIL: "The stop never moves",
    BREAKEVEN_AT_R: "Move the stop to the entry price once the trade is this far ahead",
    ATR_TRAIL: "Follow price at an ATR distance, never moving backwards",
}


class ExitRules(BaseModel):
    """Where the stop goes, where the target goes, and when to give up waiting."""

    stop_rule: Literal[WIDEST_OF] = WIDEST_OF
    # None means "use the account-wide control". Kept nullable rather than
    # copying the account value in, so that changing the account control still
    # reaches a strategy that never expressed an opinion.
    stop_atr_multiple: float | None = Field(default=None, gt=0, le=10)
    min_stop_distance_percent: float | None = Field(default=None, ge=0, le=10)

    target_rule: Literal[RR_MULTIPLE, ATR_MULTIPLE] = RR_MULTIPLE
    # None means "use this strategy's minimum reward:risk", which is what the
    # target has always been.
    target_rr: float | None = Field(default=None, ge=0.5, le=20)
    target_atr_multiple: float = Field(default=2.0, gt=0, le=20)

    trailing_rule: Literal[NO_TRAIL, BREAKEVEN_AT_R, ATR_TRAIL] = NO_TRAIL
    trailing_trigger_r: float = Field(default=1.0, gt=0, le=10)
    trailing_atr_multiple: float = Field(default=2.0, gt=0, le=10)

    # Both optional, and both default to off because neither existed before.
    # Whichever arrives first wins.
    time_exit_minutes: int | None = Field(default=None, ge=1, le=390)
    square_off_time: str | None = None

    @model_validator(mode="after")
    def validate_square_off(self) -> "ExitRules":
        if self.square_off_time is not None:
            try:
                hour, minute = (int(part) for part in self.square_off_time.split(":"))
                time(hour, minute)
            except (ValueError, TypeError) as error:
                raise ValueError("square_off_time must be HH:MM in IST, for example 15:15") from error
        return self

    @property
    def square_off(self) -> time | None:
        if self.square_off_time is None:
            return None
        hour, minute = (int(part) for part in self.square_off_time.split(":"))
        return time(hour, minute)


DEFAULT_EXIT_RULES = ExitRules()


def from_controls(controls: dict) -> ExitRules:
    """The rules carried in a controls dict, or the defaults.

    A signal recorded before this field existed has no ``exit_rules`` key, and a
    stored value that no longer validates — a rule renamed, a bound tightened —
    falls back rather than raising. Execution refusing to manage an open
    position because its stored rules are unreadable would be a far worse
    outcome than managing it the way it has always been managed.
    """
    stored = controls.get("exit_rules")
    if not isinstance(stored, dict):
        return DEFAULT_EXIT_RULES
    try:
        return ExitRules.model_validate(stored)
    except ValueError:
        return DEFAULT_EXIT_RULES


def _decimal(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class ExitPlan:
    stop: Decimal
    target: Decimal
    risk_per_unit: Decimal


def plan(
    *,
    side: str,
    entry: Decimal,
    structural_stop: Decimal,
    atr: Decimal | None,
    rules: ExitRules,
    account_stop_atr_multiple: float,
    account_min_stop_percent: float,
    minimum_rr: float,
) -> ExitPlan | None:
    """The stop, the target and the distance between entry and stop.

    The stop is the widest of three candidates deliberately: the structural
    level the strategy found, a volatility floor, and a percent floor. Taking
    the widest means a stop is never placed so close to entry that ordinary
    noise takes it out — which looks like a strategy with a poor win rate and is
    really a strategy that was never given room.
    """
    entry = _decimal(entry)
    structural_distance = (entry - structural_stop) if side == "LONG" else (structural_stop - entry)

    atr_multiple = rules.stop_atr_multiple if rules.stop_atr_multiple is not None else account_stop_atr_multiple
    percent = (
        rules.min_stop_distance_percent if rules.min_stop_distance_percent is not None else account_min_stop_percent
    )
    atr_floor = atr * _decimal(atr_multiple) if atr is not None and atr > 0 else Decimal("0")
    percent_floor = entry * _decimal(percent) / Decimal("100")
    risk_per_unit = max(structural_distance, atr_floor, percent_floor)
    if risk_per_unit <= 0 or entry <= 0:
        return None

    if rules.target_rule == ATR_MULTIPLE and atr is not None and atr > 0:
        reward = atr * _decimal(rules.target_atr_multiple)
    else:
        # Falls through to the reward:risk target when ATR is unavailable, which
        # is the safer direction: a target computed from a missing ATR would be
        # a target at the entry price.
        reward = risk_per_unit * _decimal(rules.target_rr if rules.target_rr is not None else minimum_rr)

    if side == "LONG":
        return ExitPlan(stop=entry - risk_per_unit, target=entry + reward, risk_per_unit=risk_per_unit)
    return ExitPlan(stop=entry + risk_per_unit, target=entry - reward, risk_per_unit=risk_per_unit)


def trail_to(
    *,
    side: str,
    entry: Decimal,
    current_stop: Decimal,
    risk_per_unit: Decimal,
    candle_close: Decimal,
    candle_extreme: Decimal,
    atr: Decimal | None,
    rules: ExitRules,
) -> Decimal | None:
    """Where the stop should be now, or None if it should not move.

    ``candle_extreme`` is the high for a long and the low for a short: the best
    price the trade has seen on this candle, which is what a trail follows.

    Two invariants, both of which exist because breaking either turns a stop
    into a way of losing more than planned:

    **A stop never widens.** Every rule below returns None unless the new level
    is strictly better than the current one. A trail that could move away from
    price would increase risk on a trade already taken, which is the opposite of
    what trailing is for.

    **A stop never crosses price.** A level already through the current close
    would be filled on the next tick at a price nobody chose. When the
    arithmetic produces one, the move is refused and the existing stop stands.
    """
    if rules.trailing_rule == NO_TRAIL or risk_per_unit <= 0:
        return None

    entry, current_stop = _decimal(entry), _decimal(current_stop)
    candle_close, candle_extreme = _decimal(candle_close), _decimal(candle_extreme)
    long = side == "LONG"

    if rules.trailing_rule == BREAKEVEN_AT_R:
        # "Breakeven" here means the price paid, not zero money. Brokerage, STT,
        # GST and the rest are charged on both legs, so a trade stopped out at
        # its entry price still costs what it cost to take. Calling this
        # break-even is the market's convention, not an accounting claim.
        gained = (candle_extreme - entry) if long else (entry - candle_extreme)
        if gained < risk_per_unit * _decimal(rules.trailing_trigger_r):
            return None
        candidate = entry
    else:  # ATR_TRAIL
        if atr is None or atr <= 0:
            return None
        distance = atr * _decimal(rules.trailing_atr_multiple)
        candidate = (candle_extreme - distance) if long else (candle_extreme + distance)

    improves = candidate > current_stop if long else candidate < current_stop
    if not improves:
        return None
    crosses_price = candidate >= candle_close if long else candidate <= candle_close
    if crosses_price:
        return None
    return candidate


def time_exit_due(
    *,
    opened_at: datetime | None,
    now: datetime,
    rules: ExitRules,
) -> str | None:
    """Why this position should be closed on the clock, or None.

    Returns the reason in words rather than a bool because it is written into
    the order's snapshot, and "held for 45 minutes" and "square-off at 15:15"
    are different answers to "why did this close where it did".

    Both limits are off by default. Nothing in this system closed a position on
    time before, and switching that on for existing strategies would change what
    every past measurement meant.
    """
    if opened_at is None:
        return None

    if rules.time_exit_minutes is not None:
        held = now - opened_at.astimezone(UTC)
        if held >= timedelta(minutes=rules.time_exit_minutes):
            return f"Time exit: held for {rules.time_exit_minutes} minutes"

    square_off = rules.square_off
    if square_off is not None:
        local = now.astimezone(MARKET_TIMEZONE)
        if local.time() >= square_off:
            return f"Square-off at {rules.square_off_time} IST"

    return None
