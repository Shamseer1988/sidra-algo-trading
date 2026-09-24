"""What every trading control means, for a UI that does not hard-code it.

The settings screen used to render ``Object.entries(controls)`` as a grid of raw
inputs labelled by their snake_case key. "maximum open exposure percent" with a
bare number box, no unit, no range, no indication that the number is multiplied
by leverage before it becomes rupees. An operator could not tell ₹10,000 from
₹50,000 without reading the risk engine.

This module is the description the UI renders from. Three rules make it worth
having rather than a second place to be wrong:

**Bounds are read from the schema, never restated.** ``TradingControls`` already
declares ``gt=0, le=5``; repeating that here would eventually disagree with it,
and the disagreement would show an operator a range the server then refuses. The
spec carries only what a type cannot know — what the number means, what it is
measured in, and when a change to it starts to matter.

**Every field must be described.** A test asserts the spec list and the model's
fields are the same set. Adding a control without describing it fails the build
rather than shipping a mystery box, and deleting a control without removing its
spec fails too.

**Effect timing is stated because it is not obvious.** Some of these bite on the
next candle, some on the next signal, some not until tomorrow's session, and one
needs a restart. An operator who raises a daily loss limit at 11:00 should know
whether it applies to the day already in progress — it does, and that is a
surprising enough answer to be worth writing down.
"""

from dataclasses import dataclass
from typing import Any

import annotated_types as at

# Groups, in the order the brief asks for them.
ACCOUNT = "ACCOUNT_AND_BROKER"
DAILY_RISK = "DAILY_RISK"
SESSION = "TRADING_SESSION"
SIGNAL_QUALITY = "SIGNAL_QUALITY"
EXECUTION = "EXECUTION"
INDICATORS = "INDICATORS"

GROUP_LABELS = {
    ACCOUNT: "Account and broker",
    DAILY_RISK: "Daily risk",
    SESSION: "Trading session",
    SIGNAL_QUALITY: "Signal quality",
    EXECUTION: "Execution",
    INDICATORS: "Indicator periods",
}

# When a saved change starts to matter. Spelled out per control because the
# answers genuinely differ and guessing wrong costs a session.
IMMEDIATE = "IMMEDIATE"
NEXT_SIGNAL = "NEXT_SIGNAL"
NEXT_SESSION = "NEXT_SESSION"

EFFECT_LABELS = {
    IMMEDIATE: "Applies at once, including to the session already running.",
    NEXT_SIGNAL: "Applies to the next signal evaluated; signals already taken keep the old value.",
    NEXT_SESSION: "Applies from the next trading session.",
}

# Units, so a number is never shown bare.
INR = "INR"
PERCENT = "PERCENT"
COUNT = "COUNT"
MULTIPLE = "MULTIPLE"
POINTS = "POINTS"
RATIO = "RATIO"
TIME_IST = "TIME_IST"
CHOICE = "CHOICE"
BOOLEAN = "BOOLEAN"


@dataclass(frozen=True)
class SettingSpec:
    """One control, as an operator needs to understand it."""

    key: str
    group: str
    label: str
    help: str
    unit: str
    effect: str
    # Only for CHOICE controls; a free-text box for a value the server will
    # refuse is a box that wastes somebody's afternoon.
    choices: tuple[str, ...] = ()
    # True when the control is a ceiling rather than a target, which is a
    # distinction the UI is asked to keep visible.
    is_ceiling: bool = False

    def describe(self, model_fields: dict[str, Any], value: Any, changed_at: str | None) -> dict[str, Any]:
        """The spec, the live value and the bounds the schema declares."""
        field = model_fields.get(self.key)
        minimum = maximum = None
        exclusive_minimum = exclusive_maximum = None
        for constraint in getattr(field, "metadata", []) or []:
            if isinstance(constraint, at.Ge):
                minimum = constraint.ge
            elif isinstance(constraint, at.Gt):
                exclusive_minimum = constraint.gt
            elif isinstance(constraint, at.Le):
                maximum = constraint.le
            elif isinstance(constraint, at.Lt):
                exclusive_maximum = constraint.lt
        annotation = getattr(field, "annotation", None)
        return {
            "key": self.key,
            "group": self.group,
            "group_label": GROUP_LABELS[self.group],
            "label": self.label,
            "help": self.help,
            "unit": self.unit,
            "kind": _kind_of(annotation, self.unit),
            "choices": list(self.choices),
            "is_ceiling": self.is_ceiling,
            "effect": self.effect,
            "effect_label": EFFECT_LABELS[self.effect],
            "value": value,
            "minimum": minimum,
            "maximum": maximum,
            "exclusive_minimum": exclusive_minimum,
            "exclusive_maximum": exclusive_maximum,
            "last_changed_at": changed_at,
        }


def _kind_of(annotation: Any, unit: str) -> str:
    if unit == CHOICE:
        return "choice"
    if unit == BOOLEAN or annotation is bool:
        return "boolean"
    if unit == TIME_IST:
        return "time"
    if annotation is int:
        return "integer"
    return "number"


TRADING_CONTROL_SPECS: tuple[SettingSpec, ...] = (
    # --- account and broker ------------------------------------------------
    SettingSpec(
        key="account_capital",
        group=ACCOUNT,
        label="Account capital",
        help=(
            "The cash the strategy sizes against. Position size, the daily risk budget and the "
            "exposure ceiling are all derived from it, so changing this changes every rupee figure "
            "on this page. It is not read from the broker; it is what you have decided to allocate."
        ),
        unit=INR,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="intraday_leverage_enabled",
        group=ACCOUNT,
        label="Use intraday leverage",
        help=(
            "When off, exposure is capped at the capital above. When on, the exposure ceiling is "
            "multiplied by the leverage multiplier. Leverage is a ceiling the broker permits, not a "
            "target to reach."
        ),
        unit=BOOLEAN,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="intraday_leverage_multiplier",
        group=ACCOUNT,
        label="Leverage multiplier",
        help=(
            "Multiplies the exposure ceiling. Your broker's actual margin is the real limit and is "
            "checked per order; this cannot raise it, only stay under it."
        ),
        unit=MULTIPLE,
        effect=NEXT_SIGNAL,
        is_ceiling=True,
    ),
    SettingSpec(
        key="maximum_open_exposure_percent",
        group=ACCOUNT,
        label="Exposure ceiling",
        help=(
            "A percent of capital that is then multiplied by the leverage multiplier. 100% at 5x on "
            "₹10,000 of capital is ₹50,000 of exposure — which is exposure, never cash. The rupee "
            "figure is shown beside this field so the multiplication is not something you have to do."
        ),
        unit=PERCENT,
        effect=NEXT_SIGNAL,
        is_ceiling=True,
    ),
    SettingSpec(
        key="live_broker",
        group=ACCOUNT,
        label="Live broker",
        help=(
            "Where a live order would be sent. NONE sends nothing and is the safe default — it is a "
            "refusal, not a fallback. Changing it does not move an order already placed."
        ),
        unit=CHOICE,
        choices=("NONE", "UPSTOX", "FIRSTOCK"),
        effect=IMMEDIATE,
    ),
    SettingSpec(
        key="execution_approval_mode",
        group=ACCOUNT,
        label="Approval mode",
        help=(
            "Who authorises a live order. DISABLED sends none. TELEGRAM_APPROVAL asks you per order "
            "and expires unanswered. AUTOMATIC does not ask. This decides who signs off, not whether "
            "live trading is permitted — the readiness gates decide that."
        ),
        unit=CHOICE,
        choices=("DISABLED", "TELEGRAM_APPROVAL", "AUTOMATIC"),
        effect=IMMEDIATE,
    ),
    # --- daily risk --------------------------------------------------------
    SettingSpec(
        key="risk_per_trade_percent",
        group=DAILY_RISK,
        label="Planned risk per trade",
        help=(
            "A percent of capital, shown in rupees beside the field. This is what the trade is "
            "designed to lose if its stop is hit — an intention, not a guarantee. A gap, slippage, "
            "or a stop that cannot be placed will all exceed it."
        ),
        unit=PERCENT,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="maximum_daily_risk_percent",
        group=DAILY_RISK,
        label="Daily planned-risk budget",
        help=(
            "How much planned risk may be allocated across the whole day. Set below "
            "(risk per trade x maximum trades) it silently caps the number of trades, so the "
            "effective ceiling is shown above and a warning appears if the two disagree."
        ),
        unit=PERCENT,
        effect=IMMEDIATE,
        is_ceiling=True,
    ),
    SettingSpec(
        key="maximum_daily_trades",
        group=DAILY_RISK,
        label="Maximum trades per day",
        help=(
            "Account-wide filled entries, across every strategy and both brokers. Counted when an "
            "entry receives its first fill: partial fills count once, and a signal or a rejected "
            "order counts nothing."
        ),
        unit=COUNT,
        effect=IMMEDIATE,
        is_ceiling=True,
    ),
    SettingSpec(
        key="maximum_open_positions",
        group=DAILY_RISK,
        label="Maximum concurrent positions",
        help=(
            "How many positions may be open at the same time. Above one, a single market move can "
            "hit several stops together, so the planned risk per trade stops being the worst case."
        ),
        unit=COUNT,
        effect=IMMEDIATE,
        is_ceiling=True,
    ),
    SettingSpec(
        key="daily_loss_limit",
        group=DAILY_RISK,
        label="Daily loss stop",
        help=(
            "In rupees, not a percent — a daily stop is an amount you are willing to lose today, and "
            "should not quietly rescale when capital is edited. The implied percentage is shown "
            "beside it. Measured on realised plus unrealised P&L after charges. Reaching it closes "
            "the day and exits open positions; it latches, so a later recovery does not reopen "
            "trading. It cannot guarantee the loss stops there."
        ),
        unit=INR,
        effect=IMMEDIATE,
        is_ceiling=True,
    ),
    SettingSpec(
        key="daily_profit_target",
        group=DAILY_RISK,
        label="Daily profit stop",
        help=(
            "In rupees. Reaching it ends the day the same way the loss stop does, and latches for "
            "the same reason: a winner that gives back gains must not reopen a day already declared "
            "finished. Zero disables it."
        ),
        unit=INR,
        effect=IMMEDIATE,
        is_ceiling=True,
    ),
    # --- trading session ---------------------------------------------------
    SettingSpec(
        key="trade_start_time",
        group=SESSION,
        label="Trade start time",
        help=(
            "IST. No entry is taken before this. Set after the opening range completes, or the "
            "strategy has nothing to break out of."
        ),
        unit=TIME_IST,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="trade_cutoff_time",
        group=SESSION,
        label="Entry cutoff time",
        help=(
            "IST. No new entry after this. It does not square off what is already open — exits follow their own rules."
        ),
        unit=TIME_IST,
        effect=NEXT_SESSION,
    ),
    # --- signal quality ----------------------------------------------------
    SettingSpec(
        key="minimum_score",
        group=SIGNAL_QUALITY,
        label="Minimum signal score",
        help=(
            "Out of 100, across five components. A missing input now scores zero for its component "
            "rather than full marks, so scores are lower than they were before September 2026 and "
            "are not comparable with older sessions."
        ),
        unit=POINTS,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="minimum_rr",
        group=SIGNAL_QUALITY,
        label="Minimum reward-to-risk",
        help=(
            "A setup whose target is closer than this multiple of its stop distance is not taken. "
            "Raising it takes fewer trades and needs a lower win rate to break even; the breakeven "
            "is roughly (1 + costs) / (1 + this)."
        ),
        unit=RATIO,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="volume_multiplier",
        group=SIGNAL_QUALITY,
        label="Relative volume threshold",
        help=(
            "Volume must be at least this multiple of the instrument's baseline. Below it the volume "
            "component scores zero. The baseline needs several sessions of history; without it the "
            "input is missing, and for strategies that require volume the signal is refused outright."
        ),
        unit=MULTIPLE,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="minimum_ema_spread_percent",
        group=SIGNAL_QUALITY,
        label="Minimum EMA separation",
        help=("Below this the market is treated as choppy and no entry is taken. A hard refusal, not a score penalty."),
        unit=PERCENT,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="retest_tolerance_percent",
        group=SIGNAL_QUALITY,
        label="Retest tolerance",
        help=(
            "How close price must come back to the broken level to count as a retest. Wider takes "
            "more setups and lets weaker ones through."
        ),
        unit=PERCENT,
        effect=NEXT_SIGNAL,
    ),
    # --- execution ---------------------------------------------------------
    SettingSpec(
        key="stop_atr_multiple",
        group=EXECUTION,
        label="Stop distance in ATR",
        help=(
            "One of three stop floors. The widest of the structural stop, this ATR multiple and the "
            "minimum percent below is used, so raising it widens stops, reduces position size, and "
            "moves the target further away — the reward-to-risk is held, not the rupee target."
        ),
        unit=MULTIPLE,
        effect=NEXT_SIGNAL,
    ),
    SettingSpec(
        key="min_stop_distance_percent",
        group=EXECUTION,
        label="Minimum stop distance",
        help=(
            "The percent floor under the stop, protecting against a stop so tight that noise takes "
            "it out. Same trade-off as the ATR multiple: wider stop, smaller size, further target."
        ),
        unit=PERCENT,
        effect=NEXT_SIGNAL,
    ),
)

# --- indicator periods ----------------------------------------------------
#
# These lived in .env, which made adjusting them a file edit and a restart and
# made the current value invisible to anyone not on the NAS. They are described
# here on the same terms as everything else, with one difference worth stating
# in each help string: they change what an indicator *means*, so a session
# measured before a change is not comparable with one measured after it.
#
# All of them are NEXT_SESSION, and that is not a limitation to apologise for.
# An EMA period changed at 11:00 would have one meaning before the change and
# another after it inside one day's data, with the strategy state machine
# holding a breakout established under the old reading.

INDICATOR_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="candle_timeframe_seconds",
        group=INDICATORS,
        label="Candle timeframe",
        help=(
            "Seconds per candle. Every indicator, the opening range and the strategy state machine all "
            "work on these, so this is the most consequential number here. Changing it does not "
            "reinterpret the day's data — it is what ticks are bucketed into, so a change mid-session "
            "would corrupt the session rather than re-measure it."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="opening_range_minutes",
        group=INDICATORS,
        label="Opening range length",
        help=(
            "Minutes from the open that form the range the breakout strategy trades around. Longer "
            "ranges are wider and break out less often; shorter ones break out on noise."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="ema_fast_period",
        group=INDICATORS,
        label="Fast EMA period",
        help=(
            "Candles in the fast moving average. Must stay below the slow period — a fast average that "
            "is slower than the slow one inverts every trend signal rather than producing an error."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="ema_slow_period",
        group=INDICATORS,
        label="Slow EMA period",
        help=(
            "Candles in the slow moving average. The gap between the two is what the EMA separation "
            "threshold measures, so widening this makes the choppy-market refusal fire less often."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="atr_period",
        group=INDICATORS,
        label="ATR period",
        help=(
            "Candles in the average true range. ATR sets the stop distance and scales the breakout "
            "score, so this indirectly changes position size on every trade."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="volume_lookback_candles",
        group=INDICATORS,
        label="Volume lookback",
        help=(
            "Candles averaged for the intraday volume comparison. Short lookbacks make relative volume "
            "jumpy; long ones blunt the confirmation the strategy is asking for."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="rvol_baseline_sessions",
        group=INDICATORS,
        label="Relative-volume baseline sessions",
        help=(
            "Past sessions needed before relative volume can be computed at all. Until that many exist "
            "the input is missing, and a strategy that requires volume refuses its signals outright "
            "rather than scoring them. Lowering it produces a number sooner and a worse one."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
    SettingSpec(
        key="daily_history_sessions",
        group=INDICATORS,
        label="Daily history sessions",
        help=(
            "Past daily candles kept for the daily ATR, the universe filters and relative strength. "
            "More history is steadier and slower to backfill."
        ),
        unit=COUNT,
        effect=NEXT_SESSION,
    ),
)

SPECS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in TRADING_CONTROL_SPECS}
INDICATOR_SPECS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in INDICATOR_SPECS}
# The indicator group is deliberately absent from the trading-controls order:
# those settings live under a different key and are served by their own
# endpoint, so listing the group here would promise a section the trading
# catalogue cannot fill.
GROUP_ORDER: tuple[str, ...] = (ACCOUNT, DAILY_RISK, SESSION, SIGNAL_QUALITY, EXECUTION)
