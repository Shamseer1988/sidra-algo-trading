"""Everything about one strategy, in the order somebody asks about it.

What is it trying to do, when does it work, what does it need, where does it
enter, where does it get out, how often is it allowed to trade, what has it
changed, what has it signalled lately, and — last, because it is the question
that gets answered too confidently — is it any good.

That last section is deliberately hard to satisfy. It will not describe a
strategy as working without both out-of-sample backtest evidence and paper
forward evidence, and it says how much evidence is missing rather than
returning a number that looks like an answer. A win rate over eleven trades is
not a win rate; it is a rumour with a decimal point.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BacktestSweep,
    BacktestTrade,
    PaperPosition,
    PaperSignal,
    SettingRevision,
)
from app.services.exit_rules import (
    ATR_MULTIPLE,
    ATR_TRAIL,
    BREAKEVEN_AT_R,
    NO_TRAIL,
    ExitRules,
)
from app.services.strategy_registry import StrategyConfiguration

# How many resolved trades before a result is worth reading at all. Thirty is
# not a rigorous threshold — at thirty trades a 55% win rate still carries an
# interval wide enough to contain 40% — it is the point below which the number
# is pure noise. The verdict text says so rather than implying thirty is enough.
MIN_TRADES = 30

NOT_ENOUGH_EVIDENCE = "NOT_ENOUGH_EVIDENCE"
NEGATIVE = "NEGATIVE"
INCONCLUSIVE = "INCONCLUSIVE"
PROMISING = "PROMISING"

VERDICT_LABELS = {
    NOT_ENOUGH_EVIDENCE: "Not enough evidence",
    NEGATIVE: "Losing money",
    INCONCLUSIVE: "Inconclusive",
    PROMISING: "Promising, not proven",
}


@dataclass(frozen=True)
class StrategyProfile:
    """What a strategy is for, written once, in prose."""

    purpose: str
    regime: str
    entry: str
    does_not: str


# Written here rather than in the strategy modules because these are claims
# about intent, and a claim about intent that lives next to the code will be
# updated by whoever changes the code and believes they know what it was for.
PROFILES: dict[str, StrategyProfile] = {
    "orb-retest-v1": StrategyProfile(
        purpose=(
            "Trades the first genuine break of the opening range, but only after price comes back to "
            "the broken level and holds it. The retest is the whole strategy: a break that never returns "
            "is not taken, because the return is what distinguishes a move with participation behind it "
            "from a spike."
        ),
        regime=(
            "Wants a directional morning with real volume. In a range-bound or low-volume session the "
            "opening range is broken in both directions and neither break holds."
        ),
        entry=(
            "Price breaks the opening range, returns to within the retest tolerance of the broken level, "
            "and closes back in the direction of the break with volume above the multiple."
        ),
        does_not=(
            "It does not predict the direction of the day, and it does not trade the break itself. "
            "A day that trends straight from the open without a retest produces no signal at all."
        ),
    ),
    "vwap-pullback-v1": StrategyProfile(
        purpose=(
            "Joins an established intraday trend on a pullback to VWAP, on the theory that VWAP is where "
            "the day's average participant is even and therefore where a trend is most often defended."
        ),
        regime=(
            "Needs a session already trending away from VWAP. In a session oscillating around VWAP every "
            "touch looks like a pullback and none of them are."
        ),
        entry="Price is on the correct side of VWAP, pulls back to it, and resumes with volume.",
        does_not=(
            "It does not call reversals. A pullback that continues through VWAP is a failed signal, not "
            "an entry in the other direction."
        ),
    ),
    "ema-momentum-v1": StrategyProfile(
        purpose=(
            "Follows momentum while the fast and slow EMAs are separated and moving apart, taking the "
            "continuation rather than the turn."
        ),
        regime=(
            "Wants a trending session with EMA separation above the minimum spread. The spread floor is "
            "what keeps it out of a chop, where the two EMAs cross repeatedly and every cross is a loss."
        ),
        entry="The fast EMA is clear of the slow one by the minimum spread and price pushes in that direction.",
        does_not="It does not fade extension. A strong move is a reason to stay, not a reason to bet against it.",
    ),
    "rs-pullback-v1": StrategyProfile(
        purpose=(
            "Buys the strongest names on a pullback and sells the weakest on a bounce, measuring strength "
            "against NIFTY rather than against the instrument's own history."
        ),
        regime=(
            "Needs a market with dispersion — some names clearly leading, some clearly lagging. On a day "
            "when everything moves together, relative strength measures nothing."
        ),
        entry="Relative strength against NIFTY exceeds the threshold and price pulls back without losing that lead.",
        does_not=(
            "It does not treat a rising price as strength. A name up 1% on a day NIFTY is up 2% is weak, "
            "and this strategy would be looking to sell it."
        ),
    ),
}

GENERIC_PROFILE = StrategyProfile(
    purpose="No written description exists for this strategy type yet.",
    regime="Unknown.",
    entry="See the strategy implementation.",
    does_not="Unknown.",
)


def profile_for(strategy_type: str) -> StrategyProfile:
    return PROFILES.get(strategy_type, GENERIC_PROFILE)


# --- how the trade is left, in words -------------------------------------


def describe_exit(rules: ExitRules, *, minimum_rr: float, account_atr: float, account_percent: float) -> list[str]:
    """The exit plan as sentences, with the numbers that will actually be used.

    Resolving the blanks here rather than showing "default" matters: a strategy
    that inherits the account's ATR multiple and one that sets the same number
    itself behave identically today and differently the moment somebody changes
    the account control, and the screen has to be able to say which is which.
    """
    atr_multiple = rules.stop_atr_multiple if rules.stop_atr_multiple is not None else account_atr
    atr_source = "its own" if rules.stop_atr_multiple is not None else "the account's"
    percent = rules.min_stop_distance_percent if rules.min_stop_distance_percent is not None else account_percent
    percent_source = "its own" if rules.min_stop_distance_percent is not None else "the account's"

    lines = [
        f"Stop: the widest of the structural level, {atr_multiple}× ATR ({atr_source} multiple), "
        f"and {percent}% of the entry price ({percent_source} floor).",
    ]

    if rules.target_rule == ATR_MULTIPLE:
        lines.append(
            f"Target: {rules.target_atr_multiple}× ATR from entry, falling back to a "
            f"{rules.target_rr or minimum_rr}:1 reward:risk target when ATR is unavailable."
        )
    else:
        rr = rules.target_rr if rules.target_rr is not None else minimum_rr
        source = "its own" if rules.target_rr is not None else "the strategy's minimum"
        lines.append(f"Target: {rr}× the risk taken ({source} reward:risk).")

    if rules.trailing_rule == NO_TRAIL:
        lines.append("Trailing: none. The stop stays where it was placed.")
    elif rules.trailing_rule == BREAKEVEN_AT_R:
        lines.append(
            f"Trailing: once the trade is {rules.trailing_trigger_r}R ahead, the stop moves to the price "
            "actually paid. That is break-even on price, not on money — both legs are charged either way."
        )
    elif rules.trailing_rule == ATR_TRAIL:
        lines.append(
            f"Trailing: the stop follows price at {rules.trailing_atr_multiple}× ATR and never moves "
            "backwards. It is refused if it would land through the current price."
        )

    if rules.time_exit_minutes is None and rules.square_off_time is None:
        lines.append(
            "Time exit: none. The position is held until the stop or target is hit, or the day's limit "
            "flattens it. Nothing closes it because the session is ending."
        )
    else:
        clauses = []
        if rules.time_exit_minutes is not None:
            clauses.append(f"after {rules.time_exit_minutes} minutes")
        if rules.square_off_time is not None:
            clauses.append(f"at {rules.square_off_time} IST")
        lines.append(f"Time exit: closed {' or '.join(clauses)}, whichever comes first.")

    return lines


# --- evidence ------------------------------------------------------------


@dataclass
class Evidence:
    """One body of results: backtest or paper-forward."""

    source: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: Decimal = Decimal("0")
    gross_pnl: Decimal = Decimal("0")
    charges: Decimal = Decimal("0")
    average_r: Decimal | None = None
    from_date: date | None = None
    to_date: date | None = None
    out_of_sample: bool = False

    @property
    def win_rate_percent(self) -> Decimal | None:
        decided = self.wins + self.losses
        if not decided:
            return None
        return (Decimal(self.wins) * 100 / Decimal(decided)).quantize(Decimal("0.01"))

    @property
    def sufficient(self) -> bool:
        return self.trades >= MIN_TRADES

    @property
    def shortfall(self) -> int:
        return max(0, MIN_TRADES - self.trades)


@dataclass
class Assessment:
    """Whether the evidence supports anything, and what is missing if not."""

    verdict: str
    headline: str
    caveats: list[str] = field(default_factory=list)


def assess(backtest: Evidence, forward: Evidence) -> Assessment:
    """Judge a strategy on both bodies of evidence, and refuse to guess.

    The rule the brief asked for, stated as code: nothing is called good on
    backtest alone. A backtest is a claim about a period whose outcome was
    already known when the parameters were chosen, and the only thing that
    answers it is results the strategy has not seen.

    ``PROMISING`` is the strongest verdict available, and it is deliberately not
    "profitable". A strategy that made money over thirty trades either side has
    cleared the lowest bar worth clearing, not proven anything.
    """
    caveats: list[str] = []
    if not backtest.out_of_sample and backtest.trades:
        caveats.append(
            "The backtest evidence is in-sample: the parameters were chosen knowing how this period turned "
            "out. Run a sweep with a holdout before reading the numbers as a forecast."
        )
    if backtest.shortfall:
        caveats.append(f"Backtest needs {backtest.shortfall} more resolved trades to reach {MIN_TRADES}.")
    if forward.shortfall:
        caveats.append(f"Paper-forward needs {forward.shortfall} more resolved trades to reach {MIN_TRADES}.")
    if backtest.sufficient and forward.sufficient:
        caveats.append(
            f"{MIN_TRADES} trades is the point below which a result is noise, not the point at which it "
            "becomes reliable. A win rate over this sample still carries a wide interval."
        )

    # A losing result is reported as losing on far less evidence than a winning
    # one is reported as promising. The asymmetry is intentional: being slow to
    # believe good news costs a delayed start, being slow to believe bad news
    # costs money.
    if forward.trades and forward.net_pnl < 0:
        return Assessment(
            verdict=NEGATIVE,
            headline=f"Losing money forward: ₹{forward.net_pnl} net over {forward.trades} trades.",
            caveats=caveats,
        )

    if not backtest.sufficient or not forward.sufficient:
        return Assessment(
            verdict=NOT_ENOUGH_EVIDENCE,
            headline=(
                "Not enough evidence to say whether this works. Both an out-of-sample backtest and "
                f"paper-forward results need at least {MIN_TRADES} resolved trades each."
            ),
            caveats=caveats,
        )

    if forward.net_pnl <= 0 or backtest.net_pnl <= 0 or not backtest.out_of_sample:
        return Assessment(
            verdict=INCONCLUSIVE,
            headline="The two bodies of evidence do not agree, or one of them is flat.",
            caveats=caveats,
        )

    return Assessment(
        verdict=PROMISING,
        headline=(
            f"Promising, not proven: ₹{backtest.net_pnl} net out-of-sample over {backtest.trades} trades "
            f"and ₹{forward.net_pnl} net forward over {forward.trades}."
        ),
        caveats=caveats,
    )


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


async def backtest_evidence(session: AsyncSession, strategy_type: str) -> Evidence:
    """Every backtest trade recorded for this strategy type, netted.

    ``out_of_sample`` is true only when a sweep with a holdout exists for this
    strategy. A plain backtest over a period whose results informed the
    parameters is not out-of-sample, however many trades it produced.
    """
    rows = list((await session.scalars(select(BacktestTrade).where(BacktestTrade.strategy_id == strategy_type))).all())
    sweep = await session.scalar(
        select(BacktestSweep.id).where(
            BacktestSweep.strategy_id == strategy_type,
            BacktestSweep.status == "COMPLETED",
            BacktestSweep.validation_fraction > 0,
        )
    )
    evidence = Evidence(source="BACKTEST", out_of_sample=sweep is not None)
    if not rows:
        return evidence

    evidence.trades = len(rows)
    evidence.wins = sum(1 for row in rows if _money(row.net_pnl) > 0)
    evidence.losses = sum(1 for row in rows if _money(row.net_pnl) < 0)
    evidence.net_pnl = sum((_money(row.net_pnl) for row in rows), start=Decimal("0"))
    evidence.gross_pnl = sum((_money(row.gross_pnl) for row in rows), start=Decimal("0"))
    evidence.charges = sum((_money(row.fees_total) for row in rows), start=Decimal("0"))
    evidence.average_r = (sum((_money(row.realized_r) for row in rows), start=Decimal("0")) / len(rows)).quantize(
        Decimal("0.01")
    )
    evidence.from_date = min(row.session_date for row in rows)
    evidence.to_date = max(row.session_date for row in rows)
    return evidence


async def forward_evidence(session: AsyncSession, strategy_version: str) -> Evidence:
    """Closed paper positions for this exact strategy version.

    Keyed on the version rather than the type on purpose. A strategy whose
    parameters changed last week is, for this question, a different strategy;
    pooling its results with the old ones would report an average of two things
    nobody is running.
    """
    rows = [
        row
        for row in (
            await session.scalars(select(PaperPosition).where(PaperPosition.strategy_version == strategy_version))
        ).all()
        if row.open_quantity == 0 and row.closed_at is not None
    ]
    evidence = Evidence(source="PAPER_FORWARD", out_of_sample=True)
    if not rows:
        return evidence

    evidence.trades = len(rows)
    evidence.wins = sum(1 for row in rows if _money(row.total_pnl) > 0)
    evidence.losses = sum(1 for row in rows if _money(row.total_pnl) < 0)
    evidence.net_pnl = sum((_money(row.total_pnl) for row in rows), start=Decimal("0"))
    evidence.gross_pnl = sum((_money(row.realized_pnl) for row in rows), start=Decimal("0"))
    evidence.charges = sum((_money(row.fees_total) for row in rows), start=Decimal("0"))
    evidence.from_date = min(row.session_date for row in rows)
    evidence.to_date = max(row.session_date for row in rows)
    return evidence


# --- history and recent activity -----------------------------------------


@dataclass(frozen=True)
class VersionChange:
    at: datetime
    version: int
    changed_keys: list[str]
    risk_increased: list[str]


async def version_history(session: AsyncSession, key: str, strategy_id: str) -> list[VersionChange]:
    """Every saved revision that touched this strategy, newest first.

    The revisions table stores the whole strategy list per save, so a change to
    one strategy appears in the same row as every other strategy's unchanged
    values. The filter below is what turns that into a per-strategy history.
    """
    rows = list(
        (
            await session.scalars(
                select(SettingRevision).where(SettingRevision.key == key).order_by(SettingRevision.created_at.desc())
            )
        ).all()
    )
    history: list[VersionChange] = []
    for row in rows:
        entries = row.value if isinstance(row.value, list) else []
        mine = next((item for item in entries if isinstance(item, dict) and item.get("id") == strategy_id), None)
        if mine is None:
            continue
        touched = [key for key in (row.changed_keys or []) if key.startswith(f"{strategy_id}.")]
        if not touched:
            continue
        history.append(
            VersionChange(
                at=row.created_at,
                version=int(mine.get("version", 1)),
                changed_keys=[key.split(".", 1)[1] for key in touched],
                risk_increased=[
                    key.split(".", 1)[1] for key in (row.risk_increased or []) if key.startswith(f"{strategy_id}.")
                ],
            )
        )
    return history


async def recent_signals(session: AsyncSession, strategy_version: str, limit: int = 20) -> list[PaperSignal]:
    return list(
        (
            await session.scalars(
                select(PaperSignal)
                .where(PaperSignal.strategy_version == strategy_version)
                .order_by(PaperSignal.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def signal_activity(session: AsyncSession, strategy_version: str, days: int = 30) -> tuple[int, date | None]:
    """How many signals in the window, and when the last one was.

    A strategy that is enabled but has signalled nothing in a month is a
    different situation from one that is switched off, and the screen has no
    other way to tell them apart.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    rows = list(
        (
            await session.scalars(
                select(PaperSignal.session_date).where(
                    PaperSignal.strategy_version == strategy_version, PaperSignal.session_date >= since
                )
            )
        ).all()
    )
    return len(rows), max(rows) if rows else None


def configured_limits(configuration: StrategyConfiguration) -> dict[str, object]:
    return {
        "max_trades_per_day": configuration.max_trades_per_day,
        "max_trades_per_side": configuration.max_trades_per_side,
        "cooldown_minutes": configuration.cooldown_minutes,
        "allowed_sides": list(configuration.allowed_sides),
        "allowed_sessions": list(configuration.allowed_sessions),
        "universe_size": len(configuration.universe),
        "minimum_score": configuration.minimum_score,
        "minimum_rr": configuration.minimum_rr,
    }
