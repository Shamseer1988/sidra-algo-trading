"""Replay the swing strategies over daily history, as the paper desk would trade them.

Phase 2 of the swing desk. Pure: candles in, trades and numbers out. Nothing is
written anywhere, and nothing here is connected to an order path.

**One evening at a time, no lookahead.** After each session's close the setups
are found from candles up to that close, and become buy-stop orders for the
next session only. A plan that does not trigger the next day lapses.

**Fills a daily candle can honestly support:**

* Entry: if the next session opens more than ``gap_skip`` above the trigger the
  trade is skipped (the evening plan was priced for a different market).
  Otherwise, if the high reaches the trigger, it fills at the trigger -- or at
  the open, when the open is already above it.
* Stop: if the low reaches the stop, the exit is the stop -- or the open, when
  the session opened below it. A gap through a stop costs more than planned,
  and the backtest charges that, as the market would.
* Stop and target on the same day: the stop is assumed to have come first. A
  daily candle cannot say which did, and assuming the kinder order is how
  backtests come to promise what the market never pays.
* Half the position is sold at 2R; the stop on the rest moves to entry.
* The rest is sold at the next open after a *close* below the trailing level,
  or after the holding limit.

**After charges.** Every trade pays STT, stamp duty, exchange and SEBI fees, GST,
brokerage and the depository charge on each sale (``CostModel``). The figures are
the published rates as written down here; check them against an Upstox
contract note before trusting the rupees to the paisa.

**In-sample and out-of-sample.** Results are reported for the first 70% of the
history and the last 30% separately. The parameters were fixed before any run,
so the out-of-sample half has never been used to choose them. That half is the
one that means anything.

**Survivorship.** The universe is today's NIFTY 200. Shares that fell out of the
index over the period are missing, and they tend to be the ones that did badly,
so every figure here leans optimistic.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal

from app.services.market_calculations import CompletedCandle
from app.services.swing_strategies import STRATEGIES, Series, SwingSetup, sma, trail_level


@dataclass(frozen=True)
class CostModel:
    """Indian equity delivery charges, per executed order.

    Rates as published for NSE delivery trades. ``brokerage_per_order`` and
    ``dp_charge_per_sale`` depend on the broker plan; verify both against a real
    Upstox contract note.
    """

    brokerage_per_order: Decimal = Decimal("20")
    stt_rate: Decimal = Decimal("0.001")  # 0.1% on buy and on sell
    exchange_rate: Decimal = Decimal("0.0000297")  # NSE transaction charge
    sebi_rate: Decimal = Decimal("0.000001")  # Rs 10 per crore
    stamp_rate: Decimal = Decimal("0.00015")  # 0.015% on buy only
    gst_rate: Decimal = Decimal("0.18")  # on brokerage + exchange + SEBI
    dp_charge_per_sale: Decimal = Decimal("20")  # depository charge per scrip per sale day

    def buy(self, value: Decimal) -> Decimal:
        fees = self.brokerage_per_order + value * (self.exchange_rate + self.sebi_rate)
        return value * (self.stt_rate + self.stamp_rate) + fees * (1 + self.gst_rate)

    def sell(self, value: Decimal) -> Decimal:
        fees = self.brokerage_per_order + value * (self.exchange_rate + self.sebi_rate)
        return value * self.stt_rate + fees * (1 + self.gst_rate) + self.dp_charge_per_sale


@dataclass(frozen=True)
class SwingRules:
    """The paper desk's limits. Defaults as approved: Rs 1,00,000 of paper capital."""

    capital: Decimal = Decimal("100000")
    risk_per_trade_percent: Decimal = Decimal("0.75")
    max_open_positions: int = 5
    max_per_sector: int = 1
    heat_cap_percent: Decimal = Decimal("4")  # all open risk together
    max_position_percent: Decimal = Decimal("35")  # of capital, in one share
    gap_skip: Decimal = Decimal("0.02")
    regime_sma: int = 50  # no new entries while NIFTY closes below this average
    strategies: tuple[str, ...] = tuple(STRATEGIES)

    @property
    def risk_per_trade(self) -> Decimal:
        return self.capital * self.risk_per_trade_percent / 100

    @property
    def heat_cap(self) -> Decimal:
        return self.capital * self.heat_cap_percent / 100


@dataclass
class SwingTrade:
    token: str
    sector: str
    setup: SwingSetup
    quantity: int
    entry_date: date
    entry_price: Decimal
    stop: Decimal
    remaining: int = 0
    proceeds: Decimal = Decimal("0")
    sales: int = 0
    partial_done: bool = False
    partial_price: Decimal | None = None
    holding_days: int = 0
    exit_next_open: str | None = None
    exit_date: date | None = None
    exit_reason: str | None = None
    charges: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.remaining = self.quantity

    @property
    def initial_risk(self) -> Decimal:
        return (self.entry_price - self.setup.initial_stop) * self.quantity

    @property
    def open_risk(self) -> Decimal:
        return max(Decimal("0"), (self.entry_price - self.stop) * self.remaining)

    @property
    def gross_pnl(self) -> Decimal:
        return self.proceeds - self.entry_price * self.quantity

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.charges

    @property
    def r_multiple(self) -> Decimal:
        return self.net_pnl / self.initial_risk if self.initial_risk else Decimal("0")


@dataclass
class BacktestResult:
    trades: list[SwingTrade] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    dates: list[date] = field(default_factory=list)


def _sell(trade: SwingTrade, quantity: int, price: Decimal, costs: CostModel) -> None:
    value = price * quantity
    trade.proceeds += value
    trade.charges += costs.sell(value)
    trade.remaining -= quantity
    trade.sales += 1


def _close(trade: SwingTrade, price: Decimal, day: date, reason: str, costs: CostModel) -> None:
    _sell(trade, trade.remaining, price, costs)
    trade.exit_date, trade.exit_reason = day, reason


def advance_open(trade: SwingTrade, series: Series, i: int, costs: CostModel) -> bool:
    """One session for a position already held. Returns True when it closed.

    Pending exits at the open first, then the stop, then the 2R target; after the
    close, count the day and decide whether tomorrow's open sells the rest.
    """
    bar = series.bars[i]
    if trade.exit_next_open:
        _close(trade, bar.open, bar.session_date, trade.exit_next_open, costs)
    elif bar.low <= trade.stop:
        reason = "stop" if not trade.partial_done else "breakeven stop"
        _close(trade, min(bar.open, trade.stop), bar.session_date, reason, costs)
    elif not trade.partial_done and bar.high >= trade.setup.target:
        half = trade.remaining // 2
        if half:
            trade.partial_price = max(bar.open, trade.setup.target)
            _sell(trade, half, trade.partial_price, costs)
        trade.partial_done = True
        trade.stop = max(trade.stop, trade.entry_price)
    if trade.exit_date:
        return True
    trade.holding_days += 1
    level = trail_level(series, i, trade.setup.trail)
    if trade.partial_done and level is not None and float(bar.close) < level:
        trade.exit_next_open = "trail"
    elif trade.holding_days >= trade.setup.max_holding_days:
        trade.exit_next_open = "time"
    return False


def fill_plan(
    token: str,
    sector: str,
    setup: SwingSetup,
    quantity: int,
    bar: CompletedCandle,
    rules: SwingRules,
    costs: CostModel,
) -> tuple[SwingTrade | None, str | None]:
    """Yesterday evening's plan against today's candle: the trade, or why there is none."""
    if bar.open > setup.entry_trigger * (1 + rules.gap_skip):
        return None, "gapped above trigger"
    if bar.high < setup.entry_trigger:
        return None, "not triggered"
    price = max(bar.open, setup.entry_trigger)
    trade = SwingTrade(token, sector, setup, quantity, bar.session_date, price, setup.initial_stop)
    trade.charges += costs.buy(price * quantity)
    if bar.low <= trade.stop:
        # Filled and stopped the same day: a daily candle cannot say the order,
        # so the losing one is assumed.
        _close(trade, trade.stop, bar.session_date, "stop same day", costs)
    return trade, None


def find_setups(
    series: dict[str, Series],
    position_of: dict[str, dict[date, int]],
    bench_close: dict,
    day: date,
    strategies: dict[str, Callable],
) -> list[tuple[str, SwingSetup]]:
    """Every setup found on this evening's close, best-ranked first."""
    found: list[tuple[str, SwingSetup]] = []
    for token, s in series.items():
        i = position_of[token].get(day)
        if i is None:
            continue
        for fn in strategies.values():
            setup = fn(s, bench_close, i)
            if setup is not None:
                found.append((token, setup))
    found.sort(key=lambda item: item[1].rank, reverse=True)
    return found


def size(setup: SwingSetup, rules: SwingRules) -> int:
    """Shares: the trade's risk over the stop distance, capped by the share of capital one position may take."""
    by_risk = int((rules.risk_per_trade / setup.risk_per_share).to_integral_value(rounding=ROUND_DOWN))
    by_value = int(
        (rules.capital * rules.max_position_percent / 100 / setup.entry_trigger).to_integral_value(rounding=ROUND_DOWN)
    )
    return min(by_risk, by_value)


def choose_plans(
    found: Sequence[tuple[str, SwingSetup]],
    open_trades: Sequence[SwingTrade],
    sectors: dict[str, str],
    rules: SwingRules,
    *,
    avoid: dict[str, str] | None = None,
) -> tuple[list[tuple[str, SwingSetup, int]], list[tuple[str, SwingSetup, str]]]:
    """Which setups become tomorrow's orders, inside the desk's limits, and why the rest do not.

    ``avoid`` maps a share to a reason it must not be bought now -- a results
    date close ahead, on the paper desk. The backtest has no such calendar.
    """
    plans: list[tuple[str, SwingSetup, int]] = []
    refused: list[tuple[str, SwingSetup, str]] = []
    held = {trade.token for trade in open_trades}
    sectors_used: dict[str, int] = defaultdict(int)
    for trade in open_trades:
        sectors_used[trade.sector] += 1
    heat = sum((trade.open_risk for trade in open_trades), start=Decimal("0"))
    for token, setup in found:
        if token in held:
            continue
        if avoid and token in avoid:
            refused.append((token, setup, avoid[token]))
            continue
        if len(open_trades) + len(plans) >= rules.max_open_positions:
            refused.append((token, setup, "position limit"))
            continue
        sector = sectors.get(token, "")
        if sectors_used[sector] >= rules.max_per_sector:
            refused.append((token, setup, "sector limit"))
            continue
        quantity = size(setup, rules)
        if quantity < 1:
            refused.append((token, setup, "too dear for the risk"))
            continue
        new_risk = setup.risk_per_share * quantity
        if heat + new_risk > rules.heat_cap:
            refused.append((token, setup, "heat cap"))
            continue
        plans.append((token, setup, quantity))
        held.add(token)
        sectors_used[sector] += 1
        heat += new_risk
    return plans, refused


def regime_by_day(benchmark: Sequence[CompletedCandle], period: int) -> dict[date, bool]:
    """Whether NIFTY closed above its ``period``-day average, per session."""
    closes = [float(bar.close) for bar in benchmark]
    averages = sma(closes, period)
    return {
        bar.session_date: (avg is not None and close > avg)
        for bar, close, avg in zip(benchmark, closes, averages, strict=False)
    }


def run_backtest(
    universe: dict[str, Sequence[CompletedCandle]],
    sectors: dict[str, str],
    benchmark: Sequence[CompletedCandle],
    *,
    rules: SwingRules | None = None,
    costs: CostModel | None = None,
    strategies: dict[str, Callable] | None = None,
) -> BacktestResult:
    """The same steps the paper desk takes each evening, replayed over the history."""
    rules = rules or SwingRules()
    costs = costs or CostModel()
    chosen = {name: fn for name, fn in (strategies or STRATEGIES).items() if name in rules.strategies}

    series = {token: Series.of(bars) for token, bars in universe.items() if bars}
    position_of = {token: {bar.session_date: i for i, bar in enumerate(s.bars)} for token, s in series.items()}
    bench_close = {bar.session_date: float(bar.close) for bar in benchmark}
    regime_ok = regime_by_day(benchmark, rules.regime_sma)

    result = BacktestResult(dates=[bar.session_date for bar in benchmark])
    open_trades: list[SwingTrade] = []
    plans: list[tuple[str, SwingSetup, int]] = []

    for day in result.dates:
        # 1. Positions already held.
        for trade in list(open_trades):
            i = position_of[trade.token].get(day)
            if i is not None and advance_open(trade, series[trade.token], i, costs):
                open_trades.remove(trade)
                result.trades.append(trade)

        # 2. Yesterday evening's plans: fill, skip on a gap, or lapse.
        for token, setup, quantity in plans:
            i = position_of[token].get(day)
            if i is None:
                result.skipped["no candle"] += 1
                continue
            trade, reason = fill_plan(
                token, sectors.get(token, ""), setup, quantity, series[token].bars[i], rules, costs
            )
            if trade is None:
                result.skipped[reason] += 1
            elif trade.exit_date:
                result.trades.append(trade)
            else:
                open_trades.append(trade)
        plans = []

        # 3. This evening: new setups for tomorrow, inside the desk's limits.
        if not regime_ok.get(day):
            continue
        plans, refused = choose_plans(
            find_setups(series, position_of, bench_close, day, chosen), open_trades, sectors, rules
        )
        for _token, _setup, reason in refused:
            result.skipped[reason] += 1

    # Anything still open at the end is marked at the last close, and said so.
    for trade in open_trades:
        bars = series[trade.token].bars
        _close(trade, bars[-1].close, bars[-1].session_date, "open at end", costs)
        result.trades.append(trade)
    return result


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    win_rate: float
    net_pnl: Decimal
    charges: Decimal
    expectancy: Decimal  # net rupees per trade
    average_r: Decimal
    profit_factor: float | None
    max_drawdown: Decimal
    average_holding_days: float


def metrics(trades: Sequence[SwingTrade]) -> Metrics:
    ordered = sorted(trades, key=lambda trade: (trade.exit_date, trade.entry_date))
    count = len(ordered)
    wins = sum(1 for trade in ordered if trade.net_pnl > 0)
    net = sum((trade.net_pnl for trade in ordered), start=Decimal("0"))
    gains = sum((trade.net_pnl for trade in ordered if trade.net_pnl > 0), start=Decimal("0"))
    losses = -sum((trade.net_pnl for trade in ordered if trade.net_pnl < 0), start=Decimal("0"))
    equity = peak = drawdown = Decimal("0")
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Metrics(
        trades=count,
        wins=wins,
        win_rate=wins / count if count else 0.0,
        net_pnl=net.quantize(Decimal("0.01")),
        charges=sum((trade.charges for trade in ordered), start=Decimal("0")).quantize(Decimal("0.01")),
        expectancy=(net / count).quantize(Decimal("0.01")) if count else Decimal("0"),
        average_r=(sum((trade.r_multiple for trade in ordered), start=Decimal("0")) / count).quantize(Decimal("0.01"))
        if count
        else Decimal("0"),
        profit_factor=float(gains / losses) if losses else None,
        max_drawdown=drawdown.quantize(Decimal("0.01")),
        average_holding_days=sum(trade.holding_days for trade in ordered) / count if count else 0.0,
    )


def split(result: BacktestResult, fraction: float = 0.7) -> tuple[date | None, list[SwingTrade], list[SwingTrade]]:
    """In-sample and out-of-sample trades, split by entry date at ``fraction`` of the history."""
    if not result.dates:
        return None, list(result.trades), []
    cut = result.dates[min(len(result.dates) - 1, int(len(result.dates) * fraction))]
    inside = [trade for trade in result.trades if trade.entry_date < cut]
    outside = [trade for trade in result.trades if trade.entry_date >= cut]
    return cut, inside, outside
