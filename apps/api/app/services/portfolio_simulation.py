"""One account, one day at a time: which of the replay's candidate trades live would have taken.

``run_completed_candle_backtest`` replays every strategy on every share on its
own. Live does not trade like that. It takes the first signals of the morning
until a ceiling closes the day -- three trades on the account, one per share,
one per sector, a strategy's own cap and cooldown -- and the live risk engine
then refuses an order the day's risk budget, open positions or exposure cannot
carry, and stops for the day once the loss limit is reached. A backtest that
takes every signal measures a different trade list from the one the account
would have produced, and on 20 November the selection study showed that list is
not a random sample of it.

So the replay runs in ``all_signals`` mode and this module walks each session's
candidates in signal order, keeping or refusing each by the rules live applies,
in the order live applies them:

  1. price band (``price_band.outside_price_band``, shared with live)
  2. the day already halted on the loss limit or profit target
  3. account daily trades          (``maximum_daily_trades``)
  4. per share                     (``maximum_daily_trades_per_instrument``)
  5. per sector                    (``maximum_daily_trades_per_sector``, ``sectors``)
  6. per strategy, then per side   (``max_trades_per_day``, ``max_trades_per_side``)
  7. strategy cooldown             (``cooldown_minutes``)
  8. daily risk budget             (``maximum_daily_risk_percent``, this trade included)
  9. open positions                (``maximum_open_positions``)
 10. open exposure                 (``maximum_open_exposure_percent`` x leverage)

**Where this is an approximation, it says so.**

* The daily loss stop is evaluated on *realised* net P&L of trades closed by
  the moment of the decision. Live reads the broker's P&L, which includes open
  positions' marks; mid-trade marks are not available from a completed-trade
  list. The halt therefore trips at the same closed losses and can trip later
  than live when an open trade is deep in loss.
* The cooldown counts the strategy's *taken* signals. Live counts its accepted
  evaluations, which also include signals a later live gate refused. The two
  differ only when a risk gate (8-10) refuses an otherwise accepted signal.
* Positions are open from the signal until their exit, so a position whose
  entry is still being filled counts against the limits, as live's in-flight
  counting does.

The two money formulas live keeps in ``live_risk`` (daily allocation budget and
exposure ceiling) are restated here rather than imported: research code must not
import the live risk engine. A test holds both to the same answers.

Pure and deterministic: no database, no Redis, no clock.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services.backtesting import BacktestResult, BacktestTradeResult, _analytics, run_completed_candle_backtest
from app.services.daily_limits import evaluate as daily_limit_reached
from app.services.price_band import outside_price_band
from app.services.strategy_registry import StrategyConfiguration

ZERO = Decimal("0")

PRICE_BAND = "price_band"
DAY_HALTED = "day_halted"
ACCOUNT_CEILING = "account_daily_trades"
SHARE_CEILING = "share_daily_trades"
SECTOR_CEILING = "sector_daily_trades"
STRATEGY_CEILING = "strategy_daily_trades"
SIDE_CEILING = "strategy_side_trades"
COOLDOWN = "strategy_cooldown"
RISK_BUDGET = "daily_risk_budget"
OPEN_POSITIONS = "open_positions"
EXPOSURE = "open_exposure"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def allocation_budget(controls: dict) -> Decimal:
    """``live_risk.allocation_budget``: capital x maximum daily risk %. Zero means unbounded."""
    return _decimal(controls.get("account_capital")) * _decimal(controls.get("maximum_daily_risk_percent")) / 100


def exposure_ceiling(controls: dict) -> Decimal:
    """``live_risk.exposure_ceiling``: capital x open-exposure % x leverage, when leverage is on."""
    leverage = (
        _decimal(controls.get("intraday_leverage_multiplier", 1))
        if controls.get("intraday_leverage_enabled", False)
        else Decimal("1")
    )
    percent = _decimal(controls.get("maximum_open_exposure_percent") or 0)
    return _decimal(controls.get("account_capital")) * percent * leverage / 100


@dataclass(frozen=True)
class StrategyLimits:
    max_trades_per_day: int
    max_trades_per_side: int | None
    cooldown_minutes: int

    @classmethod
    def of(cls, strategy: StrategyConfiguration) -> StrategyLimits:
        return cls(strategy.max_trades_per_day, strategy.max_trades_per_side, strategy.cooldown_minutes)


@dataclass(frozen=True)
class AccountLimits:
    maximum_daily_trades: int
    per_instrument: int
    per_sector: int
    maximum_open_positions: int
    risk_budget: Decimal
    exposure_ceiling: Decimal
    controls: dict

    @classmethod
    def from_controls(cls, controls: dict) -> AccountLimits:
        return cls(
            maximum_daily_trades=int(controls.get("maximum_daily_trades", 0) or 0),
            per_instrument=int(controls.get("maximum_daily_trades_per_instrument", 1) or 1),
            per_sector=int(controls.get("maximum_daily_trades_per_sector", 1) or 1),
            maximum_open_positions=int(controls.get("maximum_open_positions", 0) or 0),
            risk_budget=allocation_budget(controls),
            exposure_ceiling=exposure_ceiling(controls),
            controls=dict(controls),
        )


@dataclass
class PortfolioOutcome:
    taken: list[BacktestTradeResult]
    refused: list[tuple[BacktestTradeResult, str]]
    candidates: int
    # Candidates that never reached a decision because an earlier signal of
    # the same chain was taken: live would never have produced them.
    superseded: int
    halted_sessions: dict[str, str] = field(default_factory=dict)

    def refusal_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(reason for _, reason in self.refused).items()))

    def summary(self) -> dict:
        return {
            "candidates": self.candidates,
            "taken": len(self.taken),
            "refused": len(self.refused),
            "superseded": self.superseded,
            "refused_by_reason": self.refusal_counts(),
            "halted_sessions": len(self.halted_sessions),
        }


def _own_group(token: str) -> str:
    # The live fallback when a name cannot be read: every share its own group,
    # so the sector ceiling stops protecting and never starts refusing.
    return f"UNMAPPED:{token}"


def simulate_portfolio(
    candidates: Iterable[BacktestTradeResult],
    controls: dict,
    strategies: Iterable[StrategyConfiguration],
    *,
    sector_of_token: Callable[[str], str] | None = None,
) -> PortfolioOutcome:
    """Keep the candidates one live account would have taken, and say why the rest were not.

    ``strategies`` supplies each strategy's own limits by id; a candidate whose
    strategy is not listed is refused nothing at the strategy level. Every
    candidate is treated as one account: pass one strategy at a time to measure
    each on its own (the sweep does, one variant at a time).
    """
    limits = AccountLimits.from_controls(controls)
    per_strategy = {item.id: StrategyLimits.of(item) for item in strategies}
    group_of = sector_of_token or _own_group
    band_controls = SimpleNamespace(**controls)

    by_session: dict[str, list[BacktestTradeResult]] = defaultdict(list)
    count = 0
    for trade in candidates:
        by_session[trade.session_date].append(trade)
        count += 1

    outcome = PortfolioOutcome(taken=[], refused=[], candidates=count, superseded=0)
    for session_date in sorted(by_session):
        _one_session(
            sorted(by_session[session_date], key=_decision_order),
            limits,
            per_strategy,
            group_of,
            band_controls,
            outcome,
            session_date,
        )
    return outcome


def _decision_order(trade: BacktestTradeResult):  # noqa: ANN202
    # Signal time first; ties in a deterministic order. Live breaks ties by
    # whichever candle callback reaches the entry lock first, which no replay
    # can reproduce, so the replay fixes one order and keeps it.
    return (trade.signal_at, trade.strategy_id, trade.instrument_token, trade.sequence)


def _one_session(  # noqa: PLR0913 - the session's whole state, passed explicitly
    trades: list[BacktestTradeResult],
    limits: AccountLimits,
    per_strategy: dict[str, StrategyLimits],
    group_of: Callable[[str], str],
    band_controls: SimpleNamespace,
    outcome: PortfolioOutcome,
    session_date: str,
) -> None:
    taken: list[BacktestTradeResult] = []
    taken_chains: set[str] = set()
    halted: str | None = None

    for trade in trades:
        if trade.chain_key and trade.chain_key in taken_chains:
            outcome.superseded += 1
            continue
        now = trade.signal_at

        reason = _refusal(trade, now, taken, limits, per_strategy, group_of, band_controls, halted)
        if reason is None and halted is None:
            realised = sum((item.net_pnl for item in taken if item.exited_at <= now), start=ZERO)
            halted = daily_limit_reached(realised, band_controls)
            if halted is not None:
                outcome.halted_sessions[session_date] = halted
                reason = DAY_HALTED
        if reason is not None:
            outcome.refused.append((trade, reason))
            continue
        taken.append(trade)
        if trade.chain_key:
            taken_chains.add(trade.chain_key)
    outcome.taken.extend(taken)


def _refusal(  # noqa: PLR0911, PLR0913 - one return per rule, in live's order
    trade: BacktestTradeResult,
    now,  # noqa: ANN001
    taken: list[BacktestTradeResult],
    limits: AccountLimits,
    per_strategy: dict[str, StrategyLimits],
    group_of: Callable[[str], str],
    band_controls: SimpleNamespace,
    halted: str | None,
) -> str | None:
    if outside_price_band(trade.entry_price, band_controls):
        return PRICE_BAND
    if halted is not None:
        return DAY_HALTED

    if limits.maximum_daily_trades and len(taken) >= limits.maximum_daily_trades:
        return ACCOUNT_CEILING
    if sum(1 for item in taken if item.instrument_token == trade.instrument_token) >= limits.per_instrument:
        return SHARE_CEILING
    group = group_of(trade.instrument_token)
    in_group = {
        item.instrument_token
        for item in taken
        if item.instrument_token != trade.instrument_token and group_of(item.instrument_token) == group
    }
    if len(in_group) >= limits.per_sector:
        return SECTOR_CEILING

    own = per_strategy.get(trade.strategy_id)
    if own is not None:
        mine = [item for item in taken if item.strategy_id == trade.strategy_id]
        if len(mine) >= own.max_trades_per_day:
            return STRATEGY_CEILING
        if own.max_trades_per_side and sum(1 for item in mine if item.side == trade.side) >= own.max_trades_per_side:
            return SIDE_CEILING
        if own.cooldown_minutes and mine:
            latest = max(item.signal_at for item in mine)
            if now - latest < timedelta(minutes=own.cooldown_minutes):
                return COOLDOWN

    if limits.risk_budget > 0:
        allocated = sum((item.risk_amount for item in taken), start=ZERO)
        if allocated + trade.risk_amount > limits.risk_budget:
            return RISK_BUDGET

    open_now = [item for item in taken if item.exited_at > now]
    if limits.maximum_open_positions and len(open_now) >= limits.maximum_open_positions:
        return OPEN_POSITIONS
    if limits.exposure_ceiling > 0:
        exposure = sum((item.entry_price * item.quantity for item in open_now), start=ZERO)
        if exposure + trade.entry_price * trade.quantity > limits.exposure_ceiling:
            return EXPOSURE
    return None


def run_portfolio_backtest(
    candles_by_instrument: dict,
    benchmark_candles: list,
    strategies: list[StrategyConfiguration],
    controls: dict,
    execution_controls,  # noqa: ANN001 - PaperExecutionControls
    settings,  # noqa: ANN001 - Settings or a namespace carrying the indicator periods
    *,
    sector_of_token: Callable[[str], str] | None = None,
) -> tuple[BacktestResult, PortfolioOutcome]:
    """Replay every signal, then keep what one live account would have taken.

    The result's ``trades`` are the taken trades only, and its summary carries
    a ``portfolio`` block: candidates, taken, refused by reason, halted days.
    All the strategies passed share one account, as they do live.
    """
    replay = run_completed_candle_backtest(
        candles_by_instrument,
        benchmark_candles,
        strategies,
        controls,
        execution_controls,
        settings,
        all_signals=True,
    )
    outcome = simulate_portfolio(replay.trades, controls, strategies, sector_of_token=sector_of_token)
    taken = sorted(outcome.taken, key=lambda trade: (trade.exited_at, trade.trade_key))
    summary = {**_analytics(taken, _decimal(controls["account_capital"])), "portfolio": outcome.summary()}
    return (
        BacktestResult(
            trades=tuple(taken),
            summary=summary,
            data_fingerprint=replay.data_fingerprint,
            source_candle_count=replay.source_candle_count,
        ),
        outcome,
    )
