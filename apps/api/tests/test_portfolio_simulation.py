"""The portfolio-day simulator keeps what one live account would have kept.

Each test builds candidate trades by hand, so the expected answer is written
down rather than computed by the code under test.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services import portfolio_simulation as sim
from app.services.backtesting import BacktestTradeResult
from app.services.strategy_registry import StrategyConfiguration

DAY = "2026-11-02"
OPEN = datetime(2026, 11, 2, 4, 15, tzinfo=UTC)  # 09:45 IST


def _trade(
    token: str,
    minute: int,
    *,
    strategy: str = "s1",
    side: str = "LONG",
    hold: int = 30,
    net: str = "100",
    risk: str = "250",
    price: str = "500",
    quantity: int = 10,
    sequence: int = 0,
    day: str = DAY,
) -> BacktestTradeResult:
    signal = OPEN + timedelta(minutes=minute)
    return BacktestTradeResult(
        trade_key=f"{strategy}:{day}:{token}:{minute}",
        strategy_id=strategy,
        strategy_name=strategy,
        strategy_version=1,
        instrument_token=token,
        session_date=day,
        side=side,
        quantity=quantity,
        signal_at=signal,
        entered_at=signal + timedelta(minutes=1),
        exited_at=signal + timedelta(minutes=hold),
        entry_price=Decimal(price),
        exit_price=Decimal(price),
        gross_pnl=Decimal(net),
        fees_total=Decimal("0"),
        net_pnl=Decimal(net),
        realized_r=Decimal(net) / Decimal(risk),
        exit_reason="TARGET",
        risk_amount=Decimal(risk),
        chain_key=f"{strategy}:{day}:{token}",
        sequence=sequence,
    )


def _controls(**extra) -> dict:
    base = {
        "account_capital": 50_000,
        "maximum_daily_trades": 3,
        "maximum_daily_trades_per_instrument": 1,
        "maximum_daily_trades_per_sector": 1,
        "maximum_open_positions": 3,
        "maximum_daily_risk_percent": 1.5,
        "maximum_open_exposure_percent": 100,
        "intraday_leverage_enabled": True,
        "intraday_leverage_multiplier": 5,
        "daily_loss_limit": 750,
        "daily_profit_target": 0,
        "universe_max_share_price": 2000,
        "universe_min_share_price": 0,
    }
    return {**base, **extra}


def _strategy(id_: str = "s1", **extra) -> StrategyConfiguration:
    values = {"max_trades_per_day": 3, **extra}
    return StrategyConfiguration(id=id_, name=f"Strategy {id_}", strategy_type="rs-pullback-v1", **values)


def _reasons(outcome) -> list[str]:
    return [reason for _, reason in outcome.refused]


# --- the money formulas match the live engine's ----------------------------------


def test_budget_and_exposure_formulas_match_live_risk():
    from app.services import live_risk  # the test may import it; the simulator must not

    for controls in (_controls(), _controls(intraday_leverage_enabled=False), _controls(maximum_daily_risk_percent=0)):
        live = SimpleNamespace(**controls)
        assert sim.allocation_budget(controls) == live_risk.allocation_budget(live)
        assert sim.exposure_ceiling(controls) == live_risk.exposure_ceiling(live)


def test_the_simulator_does_not_import_the_live_engine():
    source = sim.__file__
    text = open(source, encoding="utf-8").read()
    for forbidden in ("live_risk", "live_execution", "upstox_orders", "broker_adapter", "live_entry_bridge"):
        assert f"import {forbidden}" not in text and f"services.{forbidden}" not in text


# --- ceilings, in live's order -------------------------------------------------------


def test_the_account_ceiling_takes_the_first_three():
    candidates = [_trade(f"T{i}", minute=i, hold=1, risk="100") for i in range(5)]
    outcome = sim.simulate_portfolio(candidates, _controls(), [_strategy()])
    assert [t.instrument_token for t in outcome.taken] == ["T0", "T1", "T2"]
    assert _reasons(outcome) == [sim.ACCOUNT_CEILING, sim.ACCOUNT_CEILING]


def test_one_trade_per_share_across_strategies():
    candidates = [_trade("A", 0, strategy="s1", hold=1, risk="100"), _trade("A", 5, strategy="s2", hold=1, risk="100")]
    outcome = sim.simulate_portfolio(candidates, _controls(), [_strategy("s1"), _strategy("s2")])
    assert len(outcome.taken) == 1 and _reasons(outcome) == [sim.SHARE_CEILING]


def test_one_trade_per_sector_when_the_map_groups_them():
    groups = {"SBIN": "PSU_BANK", "PNB": "PSU_BANK", "INFY": "IT"}
    candidates = [
        _trade("SBIN", 0, hold=1, risk="100"),
        _trade("PNB", 1, hold=1, risk="100"),
        _trade("INFY", 2, hold=1, risk="100"),
    ]
    outcome = sim.simulate_portfolio(candidates, _controls(), [_strategy()], sector_of_token=groups.get)
    assert [t.instrument_token for t in outcome.taken] == ["SBIN", "INFY"]
    assert _reasons(outcome) == [sim.SECTOR_CEILING]


def test_without_a_sector_map_every_share_is_its_own_group():
    candidates = [_trade("SBIN", 0, hold=1, risk="100"), _trade("PNB", 1, hold=1, risk="100")]
    outcome = sim.simulate_portfolio(candidates, _controls(), [_strategy()])
    assert len(outcome.taken) == 2


def test_strategy_cap_side_cap_and_cooldown_are_honoured():
    controls = _controls(maximum_daily_trades=10, maximum_open_positions=10, maximum_daily_risk_percent=10)
    capped = sim.simulate_portfolio(
        [_trade(f"T{i}", i, hold=1, risk="100") for i in range(4)], controls, [_strategy(max_trades_per_day=2)]
    )
    assert len(capped.taken) == 2 and set(_reasons(capped)) == {sim.STRATEGY_CEILING}

    sides = sim.simulate_portfolio(
        [
            _trade("A", 0, side="SHORT", hold=1, risk="100"),
            _trade("B", 1, side="SHORT", hold=1, risk="100"),
            _trade("C", 2, side="LONG", hold=1, risk="100"),
        ],
        controls,
        [_strategy(max_trades_per_side=1)],
    )
    assert [t.instrument_token for t in sides.taken] == ["A", "C"] and _reasons(sides) == [sim.SIDE_CEILING]

    cooled = sim.simulate_portfolio(
        [_trade("A", 0, hold=1, risk="100"), _trade("B", 10, hold=1, risk="100"), _trade("C", 31, hold=1, risk="100")],
        controls,
        [_strategy(cooldown_minutes=30)],
    )
    assert [t.instrument_token for t in cooled.taken] == ["A", "C"] and _reasons(cooled) == [sim.COOLDOWN]


def test_the_daily_risk_budget_counts_the_candidate_itself():
    # 1.5% of 50,000 is 750: two trades of 300 fit, a third would make 900.
    candidates = [_trade(f"T{i}", i, hold=1, risk="300") for i in range(3)]
    outcome = sim.simulate_portfolio(candidates, _controls(), [_strategy()])
    assert len(outcome.taken) == 2 and _reasons(outcome) == [sim.RISK_BUDGET]


def test_open_positions_and_exposure():
    controls = _controls(maximum_daily_trades=10, maximum_open_positions=2, maximum_daily_risk_percent=10)
    held = sim.simulate_portfolio(
        [
            _trade("A", 0, hold=60, risk="100"),
            _trade("B", 1, hold=60, risk="100"),
            _trade("C", 2, hold=60, risk="100"),
            _trade("D", 90, hold=5, risk="100"),
        ],
        controls,
        [_strategy(max_trades_per_day=10)],
    )
    assert [t.instrument_token for t in held.taken] == ["A", "B", "D"]  # D after A and B have closed
    assert _reasons(held) == [sim.OPEN_POSITIONS]

    tight = _controls(
        maximum_daily_trades=10,
        maximum_open_positions=10,
        intraday_leverage_enabled=False,
        maximum_open_exposure_percent=10,
    )  # 5,000 of exposure
    exposed = sim.simulate_portfolio(
        [
            _trade("A", 0, hold=60, risk="100", price="400", quantity=10),
            _trade("B", 1, hold=60, risk="100", price="200", quantity=10),
        ],
        tight,
        [_strategy()],
    )
    assert [t.instrument_token for t in exposed.taken] == ["A"] and _reasons(exposed) == [sim.EXPOSURE]


def test_the_loss_limit_stops_the_day_on_closed_losses():
    controls = _controls(
        maximum_daily_trades=10, maximum_open_positions=10, maximum_daily_risk_percent=10, daily_loss_limit=750
    )
    candidates = [
        _trade("A", 0, hold=5, net="-400", risk="100"),
        _trade("B", 1, hold=5, net="-400", risk="100"),
        _trade("C", 10, hold=5, net="500", risk="100"),  # both losses closed by now: -800
        _trade("D", 20, hold=5, net="500", risk="100"),
    ]
    outcome = sim.simulate_portfolio(candidates, controls, [_strategy(max_trades_per_day=10)])
    assert [t.instrument_token for t in outcome.taken] == ["A", "B"]
    assert _reasons(outcome) == [sim.DAY_HALTED, sim.DAY_HALTED]
    assert outcome.halted_sessions == {DAY: "Daily loss limit reached"}

    # The next day starts fresh.
    tomorrow = sim.simulate_portfolio(
        [*candidates, _trade("E", 0, day="2026-11-03", risk="100")], controls, [_strategy(max_trades_per_day=10)]
    )
    assert any(t.session_date == "2026-11-03" for t in tomorrow.taken)


def test_the_price_band_refuses_dear_shares():
    outcome = sim.simulate_portfolio([_trade("MRF", 0, price="2500")], _controls(), [_strategy()])
    assert outcome.taken == [] and _reasons(outcome) == [sim.PRICE_BAND]


# --- chains: a refused signal may form again, a taken one never does -------------------


def test_a_refused_signal_can_be_taken_when_it_forms_again():
    controls = _controls(maximum_daily_trades=10, maximum_open_positions=1, maximum_daily_risk_percent=10)
    candidates = [
        _trade("A", 0, hold=30, risk="100"),
        _trade("B", 5, hold=10, risk="100", sequence=0),  # refused: A still open
        _trade("B", 40, hold=10, risk="100", sequence=1),  # forms again after A closed
    ]
    outcome = sim.simulate_portfolio(candidates, controls, [_strategy(max_trades_per_day=10)])
    assert [(t.instrument_token, t.sequence) for t in outcome.taken] == [("A", 0), ("B", 1)]


def test_after_a_chain_is_taken_its_later_signals_never_happened():
    controls = _controls(maximum_daily_trades=10, maximum_daily_trades_per_instrument=5)
    candidates = [_trade("A", 0, hold=5, risk="100", sequence=0), _trade("A", 30, hold=5, risk="100", sequence=1)]
    outcome = sim.simulate_portfolio(candidates, controls, [_strategy()])
    assert len(outcome.taken) == 1 and outcome.superseded == 1 and outcome.refused == []


def test_the_result_is_independent_of_input_order():
    candidates = [_trade(f"T{i}", i, hold=1, risk="100") for i in range(5)]
    forward = sim.simulate_portfolio(candidates, _controls(), [_strategy()])
    backward = sim.simulate_portfolio(list(reversed(candidates)), _controls(), [_strategy()])
    assert [t.trade_key for t in forward.taken] == [t.trade_key for t in backward.taken]
    assert forward.summary() == backward.summary()
