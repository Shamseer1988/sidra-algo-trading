"""Research Lab Phase 2: the backtest measures the account live would have run.

Each claim was a gap the 27 November audit found:

* a gap through the stop was booked at the stop, a price that never traded;
* nothing closed a trade at the account's 3 PM deadline;
* ``max_trades_per_day`` and ``cooldown_minutes`` were sweepable and ignored;
* API sweeps measured with the environment's indicator periods;
* the "best" combination was chosen by its validation result;
* there was no walk-forward and no interval on an average.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import backtest_sweep
from app.services.backtest_sweep import bootstrap_interval, run_parameter_sweep, walk_forward
from app.services.backtesting import BacktestTradeResult, _exit_trade, run_completed_candle_backtest
from app.services.exit_rules import ExitRules, under_account_deadline
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.portfolio_simulation import run_portfolio_backtest
from app.services.strategy_registry import StrategyConfiguration
from tests.test_backtest_sweep import _EXECUTION, _controls, _history, _settings, _strategy

IST = ZoneInfo("Asia/Kolkata")
NO_COSTS = PaperExecutionControls(
    slippage_bps=0,
    brokerage_percent=0,
    stt_sell_percent=0,
    exchange_transaction_percent=0,
    gst_percent=0,
    sebi_percent=0,
    stamp_duty_buy_percent=0,
    ipft_percent=0,
    stop_limit_band_percent=2.0,
)


def _bar(hhmm: str, o: str, h: str, low: str, c: str, day: date = date(2026, 11, 2)) -> CompletedCandle:
    hour, minute = (int(part) for part in hhmm.split(":"))
    opened = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)
    return CompletedCandle(
        instrument_token="NSE:TEST",
        timeframe_seconds=60,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=1_000,
        tick_count=10,
    )


LONG = SimpleNamespace(side="LONG", entry_price=Decimal("100"), stop_price=Decimal("99"), target_price=Decimal("110"))


def _exit(candles, rules=None):
    return _exit_trade(LONG, candles, 0, NO_COSTS, entry_price=Decimal("100"), rules=rules or ExitRules())


# --- stop fills ------------------------------------------------------------------------


def test_a_stop_traded_through_fills_at_the_stop():
    price, _, reason = _exit([_bar("10:00", "100", "100", "100", "100"), _bar("10:01", "100", "100.5", "98.5", "99")])
    assert (price, reason) == (Decimal("99.0000"), "STOP")


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    price, _, reason = _exit([_bar("10:00", "100", "100", "100", "100"), _bar("10:01", "98", "98.4", "97.5", "98")])
    assert reason == "STOP" and price == Decimal("98.0000")  # was 99, a price that never traded


def test_a_gap_past_the_band_rests_until_price_returns_to_the_band_edge():
    candles = [
        _bar("10:00", "100", "100", "100", "100"),
        _bar("10:01", "96", "96.5", "95.5", "96"),  # beyond 99 x 0.98 = 97.02 and never back to it
        _bar("10:02", "96.5", "97.5", "96.2", "97.4"),  # trades back up to 97.02
    ]
    price, exited_at, reason = _exit(candles)
    assert reason == "STOP" and price == Decimal("97.0200")
    assert exited_at == candles[2].closed_at


# --- the account deadline ------------------------------------------------------------------


def test_the_account_deadline_closes_a_trade_the_strategy_left_open():
    rules = under_account_deadline(ExitRules(), "15:00")
    candles = [
        _bar("14:58", "100", "100.5", "99.5", "100"),
        _bar("14:59", "100", "100.5", "99.5", "100.2"),  # closes at 15:00: due
        _bar("15:00", "100.3", "100.6", "99.6", "100.4"),  # filled at this open
        _bar("15:01", "100.4", "100.8", "99.8", "100.6"),
    ]
    price, exited_at, reason = _exit(candles, rules)
    assert reason == "TIME" and price == Decimal("100.3000") and exited_at == candles[2].opened_at


def test_without_a_deadline_the_old_behaviour_holds_to_the_last_candle():
    candles = [_bar("14:58", "100", "100.5", "99.5", "100"), _bar("15:29", "100", "100.5", "99.5", "100.7")]
    _, _, reason = _exit(candles)
    assert reason == "END_OF_DATA"


def test_the_replay_applies_the_account_deadline_from_the_controls():
    by_instrument, benchmark = _history()
    early = {**_controls(), "session_square_off_time": "09:38"}  # inside the fixture's session
    result = run_completed_candle_backtest(by_instrument, benchmark, [_strategy()], early, _EXECUTION, _settings())
    assert result.trades, "the fixture should produce trades"
    for trade in result.trades:
        # Nothing is held to the end of the data any more. A trade entered
        # before the deadline is out by the candle after it; one entered after
        # it (the fixture's cutoff is later) is out on the first candle checked.
        assert trade.exit_reason in {"TIME", "STOP", "TARGET"}
        deadline = trade.entered_at.astimezone(IST).replace(hour=9, minute=38)
        latest = max(deadline, trade.entered_at.astimezone(IST)) + timedelta(minutes=2)
        assert trade.exited_at.astimezone(IST) <= latest


# --- every signal, then one account -------------------------------------------------------


def test_all_signals_mode_numbers_each_chain_and_never_loses_the_first_signal():
    by_instrument, benchmark = _history()
    first_only = run_completed_candle_backtest(
        by_instrument, benchmark, [_strategy()], _controls(), _EXECUTION, _settings()
    )
    every = run_completed_candle_backtest(
        by_instrument, benchmark, [_strategy()], _controls(), _EXECUTION, _settings(), all_signals=True
    )
    assert len(every.trades) >= len(first_only.trades)
    firsts = {(t.chain_key, t.signal_at) for t in every.trades if t.sequence == 0}
    assert {(t.chain_key, t.signal_at) for t in first_only.trades} == firsts
    for trade in every.trades:
        assert trade.chain_key.startswith("sweep-base:") and trade.risk_amount > 0


def test_the_portfolio_backtest_reports_what_it_refused():
    by_instrument, benchmark = _history()
    result, outcome = run_portfolio_backtest(
        by_instrument, benchmark, [_strategy()], {**_controls(), "maximum_daily_trades": 1}, _EXECUTION, _settings()
    )
    block = result.summary["portfolio"]
    assert block["taken"] == len(result.trades) == len(outcome.taken)
    assert block["candidates"] == block["taken"] + block["refused"] + block["superseded"]
    per_day: dict[str, int] = {}
    for trade in result.trades:
        per_day[trade.session_date] = per_day.get(trade.session_date, 0) + 1
    assert max(per_day.values()) == 1


# --- the sweep honours what it sweeps ------------------------------------------------------


def _candidate(strategy_id: str, day: str, token: str, minute: int) -> BacktestTradeResult:
    signal = datetime.fromisoformat(f"{day}T04:15:00+00:00") + timedelta(minutes=minute)
    return BacktestTradeResult(
        trade_key=f"{strategy_id}:{day}:{token}:{minute}",
        strategy_id=strategy_id,
        strategy_name="x",
        strategy_version=1,
        instrument_token=token,
        session_date=day,
        side="LONG",
        quantity=1,
        signal_at=signal,
        entered_at=signal + timedelta(minutes=1),
        exited_at=signal + timedelta(minutes=2),
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        gross_pnl=Decimal("1"),
        fees_total=Decimal("0"),
        net_pnl=Decimal("1"),
        realized_r=Decimal("0.1"),
        exit_reason="TARGET",
        risk_amount=Decimal("10"),
        chain_key=f"{strategy_id}:{day}:{token}",
    )


@pytest.fixture
def fake_replay(monkeypatch):
    """Three candidates a day, on different shares and twenty minutes apart, for every variant."""
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]

    def replay(candles, benchmark, strategies, controls, execution, settings, *, all_signals=False):
        assert all_signals, "the sweep must replay every signal for the portfolio pass"
        trades = [_candidate(item.id, day, f"T{n}", 20 * n) for item in strategies for day in days for n in range(3)]
        return SimpleNamespace(trades=tuple(trades))

    monkeypatch.setattr(backtest_sweep, "run_completed_candle_backtest", replay)
    return {"NSE:A": [SimpleNamespace(session_date=date.fromisoformat(day)) for day in days]}


def test_max_trades_per_day_changes_the_result_now(fake_replay):
    result = run_parameter_sweep(
        _strategy(), _controls(), _EXECUTION, _settings(), fake_replay, [], {"max_trades_per_day": [1, 3]}
    )
    counts = {
        c.parameters["max_trades_per_day"]: c.in_sample["trades"] + c.validation["trades"] for c in result.combinations
    }
    assert counts == {1: 4, 3: 12}


def test_cooldown_changes_the_result_now(fake_replay):
    result = run_parameter_sweep(
        _strategy().model_copy(update={"max_trades_per_day": 5}),
        _controls(),
        _EXECUTION,
        _settings(),
        fake_replay,
        [],
        {"cooldown_minutes": [0, 30]},
    )
    counts = {
        c.parameters["cooldown_minutes"]: c.in_sample["trades"] + c.validation["trades"] for c in result.combinations
    }
    # Signals 20 minutes apart: a 30-minute cooldown keeps the 1st and 3rd of each day.
    assert counts == {0: 12, 30: 8}


# --- choosing honestly ------------------------------------------------------------------------


def _scored(day: str, r: str) -> SimpleNamespace:
    value = Decimal(r)
    return SimpleNamespace(
        session_date=day,
        net_pnl=value * 100,
        realized_r=value,
        gross_pnl=value * 100,
        fees_total=Decimal("0"),
        risk_amount=Decimal("100"),
    )


def test_walk_forward_chooses_on_training_data_only():
    days = [date(2026, 1, d) for d in range(5, 13)]  # 8 sessions
    # Variant 0 is good early and bad late; variant 1 the reverse.
    trades = {
        0: [_scored(d.isoformat(), "1" if i < 4 else "-1") for i, d in enumerate(days)],
        1: [_scored(d.isoformat(), "-1" if i < 4 else "1") for i, d in enumerate(days)],
    }
    result = walk_forward(trades, days, train_sessions=4, test_sessions=2, capital=Decimal("50000"))
    first = result.folds[0]
    assert first.chosen_index == 0  # chosen on days 1-4, where variant 0 looked good
    assert first.out_of_sample["average_realized_r"] == -1.0  # and it then lost: no peeking at the test
    assert result.variants_tested == 2
    assert result.stitched["trades"] == sum(fold.out_of_sample["trades"] for fold in result.folds)


def test_walk_forward_refuses_empty_windows():
    with pytest.raises(ValueError):
        walk_forward({}, [], train_sessions=0, test_sessions=1, capital=Decimal("1"))


def test_the_bootstrap_interval_is_deterministic_and_needs_ten_trades():
    values = [0.5, -1, 2, -1, -1, 2, -1, 0.3, -1, 2, -1, 1.5]
    first = bootstrap_interval(values)
    assert first == bootstrap_interval(values)
    assert first[0] < sum(values) / len(values) < first[1]
    assert bootstrap_interval(values[:9]) is None


def test_metrics_carry_the_interval_and_double_costs():
    trades = [
        BacktestTradeResult(
            **{
                **_candidate("s", "2026-11-02", f"T{i}", i).__dict__,
                "fees_total": Decimal("2"),
                "gross_pnl": Decimal("5"),
                "net_pnl": Decimal("3"),
                "realized_r": Decimal("0.3"),
            }
        )
        for i in range(12)
    ]
    metrics = backtest_sweep._metrics(trades, Decimal("50000"))
    assert metrics["average_r_ci95"] == [0.3, 0.3]
    assert metrics["average_r_at_double_costs"] == pytest.approx(0.1)  # (5 - 2 x 2) / 10


# --- the API sweep measures with the stored indicator periods ---------------------------------


async def test_the_sweep_job_uses_the_stored_indicator_periods(monkeypatch):
    """The sweep runs in the research worker (Phase 3) and measures with the scanner's ruler.

    It used to pass the environment's settings, so a sweep after any change on
    the Settings screen measured with different indicator periods than the
    scanner trades with.
    """
    from app.db.session import SessionLocal
    from app.services import research_runs

    stored_periods = SimpleNamespace(marker="stored")
    seen: dict = {}

    async def effective(_session, _settings):
        return stored_periods

    async def history(_session, settings, *_args):
        seen["history"] = settings
        return {"NSE:A": []}, []

    async def sectors(_session, _tokens):
        return lambda token: token

    def sweep(_strategy, _controls, _execution, base_settings, *_args, **kwargs):
        seen["sweep"] = base_settings
        seen["sector_of_token"] = kwargs.get("sector_of_token")
        return backtest_sweep.SweepResult(combinations=(), best_index=None)

    monkeypatch.setattr(research_runs, "effective_settings", effective)
    monkeypatch.setattr(research_runs, "load_history", history)
    monkeypatch.setattr(research_runs, "sector_lookup", sectors)
    monkeypatch.setattr(research_runs, "run_parameter_sweep", sweep)

    strategy = StrategyConfiguration(id="t-sweep", name="Sweep target", strategy_type="rs-pullback-v1")
    payload = research_runs.sweep_payload(
        {
            "strategy_id": strategy.id,
            "start_date": "2026-11-02",
            "end_date": "2026-11-05",
            "instrument_tokens": ["NSE:A"],
            "parameter_grid": {"minimum_score": [60, 70]},
        },
        strategy,
        {"account_capital": 50000},
        PaperExecutionControls(),
        40,
    )
    async with SessionLocal() as session:
        result = await research_runs.run_sweep_job(session, payload, SimpleNamespace(), None)
        await session.rollback()  # nothing to keep
    assert seen["history"] is stored_periods
    assert seen["sweep"] is stored_periods
    assert seen["sector_of_token"] is not None
    assert result["combinations"] == 0
