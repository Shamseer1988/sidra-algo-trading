"""CPR First-Hour Breakout, and the paper-only switch it runs behind.

Two things are pinned here. The strategy follows the operator's notes and
money-management sheet. And ``live_orders=false`` means what it says: a
paper-only strategy's signal never reaches the broker, and its paper fills
never spend one of the live strategy's daily trades.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import PaperFill, PaperOrder, PaperSignal
from app.db.session import SessionLocal
from app.services import scanner_orchestration as module
from app.services.exit_rules import ExitRules
from app.services.extra_strategies import (
    AWAITING,
    CPR_ARMED,
    CPR_BREAKOUT_VERSION,
    SIGNALLED,
    cpr_buy_setup,
    cpr_relation,
    evaluate_cpr_breakout,
)
from app.services.market_calculations import CompletedCandle, cpr_levels
from app.services.paper_strategy import SIGNALLED as PAPER_SIGNALLED
from app.services.scanner_orchestration import PaperOutcome, PaperScannerOrchestrator
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry
from app.services.trade_counter import count_filled_entries, filled_entry_instruments

SESSION = date(2026, 11, 18)
TOKEN = "NSE_EQ|INECPRTEST"


def _daily(day: date, high: str, low: str, close: str) -> CompletedCandle:
    opened = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)  # 9:15 AM IST
    return CompletedCandle(
        instrument_token=TOKEN,
        timeframe_seconds=86400,
        opened_at=opened,
        closed_at=opened + timedelta(hours=6, minutes=15),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
        tick_count=10,
    )


# Day before yesterday: H 105 L 95 C 100 -> yesterday's CPR is a point at 100, R1 105, R2 110.
# Yesterday: H 104 L 100 C 102.5 -> today's CPR 102.00-102.33 (0.33% wide), R2 106.17.
DAILY = [
    _daily(date(2026, 11, 16), "105", "95", "100"),
    _daily(date(2026, 11, 17), "104", "100", "102.5"),
    # A row for the session itself must never be used for its own levels.
    _daily(SESSION, "500", "1", "250"),
]


def indicators(first_hour_high: float = 105.0) -> dict:
    return {
        "cpr": cpr_levels(DAILY, SESSION),
        "first_hour": {"high": first_hour_high, "low": 103.0, "complete": True},
        "atr": 0.2,
    }


def configuration(**overrides) -> StrategyConfiguration:  # noqa: ANN003
    values = dict(
        id="cpr-breakout-paper",
        name="CPR First-Hour Breakout (paper)",
        live_orders=False,
        strategy_type=CPR_BREAKOUT_VERSION,
        allowed_sides=["LONG"],
        max_trades_per_day=3,
        minimum_score=0,
        minimum_rr=2.0,
        exit_rules=ExitRules(stop_atr_multiple=0.01, min_stop_distance_percent=0.5, target_rr=2.0),
    )
    values.update(overrides)
    return StrategyConfiguration(**values)


BASE_CONTROLS = {
    "account_capital": 50_000.0,
    "risk_per_trade_percent": 0.5,
    "stop_atr_multiple": 2.1,
    "min_stop_distance_percent": 0.8,
    "trade_start_time": "09:45",
    "trade_cutoff_time": "11:30",
}


def controls() -> dict:
    return configuration().effective_controls(BASE_CONTROLS)


def candle(hour: int, minute: int, open_p: str, close: str) -> CompletedCandle:
    opened = datetime(2026, 11, 18, hour, minute, tzinfo=UTC)
    high = max(Decimal(open_p), Decimal(close)) + Decimal("0.05")
    low = min(Decimal(open_p), Decimal(close)) - Decimal("0.05")
    return CompletedCandle(
        instrument_token=TOKEN,
        timeframe_seconds=60,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=Decimal(open_p),
        high=high,
        low=low,
        close=Decimal(close),
        volume=500,
        tick_count=10,
    )


# 04:50 UTC is 10:20 AM IST.
AFTER_FIRST_HOUR = (4, 50)


# --- levels ---------------------------------------------------------------


def test_cpr_levels_come_from_the_two_days_before_the_session() -> None:
    levels = cpr_levels(DAILY, SESSION)
    today, yesterday = levels["today"], levels["yesterday"]
    assert yesterday["pivot"] == 100.0 and yesterday["r1"] == 105.0 and yesterday["r2"] == 110.0
    assert today["bc"] == 102.0 and today["tc"] == pytest.approx(102.3333, abs=1e-4)
    assert today["r2"] == pytest.approx(106.1667, abs=1e-4)
    assert levels["prior_day"] == {"high": 104.0, "low": 100.0, "close": 102.5}


def test_cpr_levels_need_two_prior_days() -> None:
    assert cpr_levels(DAILY[1:], SESSION)["today"] is None


def test_cpr_relation_names_each_arrangement() -> None:
    def levels(tc: float, bc: float) -> dict:
        return {"tc": tc, "bc": bc}

    y = levels(101, 100)
    assert cpr_relation(levels(103, 102), y) == "HIGHER"
    assert cpr_relation(levels(99, 98), y) == "LOWER"
    assert cpr_relation(levels(101.5, 100.5), y) == "OVERLAP_HIGHER"
    assert cpr_relation(levels(100.5, 99.5), y) == "OVERLAP_LOWER"
    assert cpr_relation(levels(100.8, 100.2), y) == "INSIDE"
    assert cpr_relation(levels(102, 99), y) == "OUTSIDE"


def test_the_setup_passes_on_the_notes_example() -> None:
    ok, why, levels = cpr_buy_setup(indicators())
    assert ok, why
    assert levels["relation"] == "HIGHER"


def test_a_first_hour_high_at_or_above_r2_is_refused() -> None:
    ok, why, _ = cpr_buy_setup(indicators(first_hour_high=106.5))
    assert not ok and "R2" in why


def test_a_first_hour_high_below_yesterdays_high_is_refused() -> None:
    ok, why, _ = cpr_buy_setup(indicators(first_hour_high=103.9))
    assert not ok and "yesterday's high" in why


def test_a_wide_cpr_is_refused() -> None:
    ok, why, _ = cpr_buy_setup(indicators(), narrow_percent=Decimal("0.2"))
    assert not ok and "narrow" in why


# --- the breakout ---------------------------------------------------------


def test_a_close_through_the_first_hour_high_signals_with_the_sheets_stop_and_target() -> None:
    decision = evaluate_cpr_breakout(candle(*AFTER_FIRST_HOUR, "104.9", "105.2"), indicators(), {}, controls())
    assert decision.next_state == SIGNALLED and decision.side == "LONG"
    assert decision.entry_price == Decimal("105.2")
    # 0.5% stop, 1:2 target: the account's 0.8% floor and 2.1x ATR do not widen it.
    assert float(decision.stop_price) == pytest.approx(104.674, abs=0.001)
    assert float(decision.target_price) == pytest.approx(106.252, abs=0.001)


def test_below_the_level_arms_and_a_later_close_above_signals() -> None:
    armed = evaluate_cpr_breakout(candle(*AFTER_FIRST_HOUR, "104.6", "104.8"), indicators(), {}, controls())
    assert armed.next_state == CPR_ARMED
    fired = evaluate_cpr_breakout(candle(4, 51, "105.1", "105.3"), indicators(), {}, controls(), CPR_ARMED)
    assert fired.next_state == SIGNALLED


def test_a_share_already_above_the_level_is_not_chased() -> None:
    decision = evaluate_cpr_breakout(candle(*AFTER_FIRST_HOUR, "105.4", "105.6"), indicators(), {}, controls())
    assert decision.next_state == AWAITING and "not chasing" in decision.reason


def test_nothing_before_10_15_or_after_2_45() -> None:
    early = evaluate_cpr_breakout(candle(4, 40, "104.9", "105.2"), indicators(), {}, controls())  # 10:10 AM
    late = evaluate_cpr_breakout(candle(9, 16, "104.9", "105.2"), indicators(), {}, controls())  # 2:46 PM
    assert early.next_state == AWAITING and late.next_state == AWAITING
    assert "CPR window" in early.reason and "CPR window" in late.reason


def test_the_account_trade_window_does_not_apply() -> None:
    """11:30 AM is the live strategy's cutoff, not this one's."""
    decision = evaluate_cpr_breakout(candle(7, 30, "104.9", "105.2"), indicators(), {}, controls())  # 1:00 PM
    assert decision.next_state == SIGNALLED


def test_the_registry_knows_the_type_and_keeps_it_long_only() -> None:
    assert CPR_BREAKOUT_VERSION in {item.identifier for item in StrategyRegistry.metadata()}
    decision = StrategyRegistry.evaluate(
        configuration(), candle(*AFTER_FIRST_HOUR, "104.9", "105.2"), indicators(), {}, controls(), AWAITING
    )
    assert decision.next_state == SIGNALLED and decision.side == "LONG"


# --- the paper-only switch ------------------------------------------------


def test_a_stored_strategy_without_the_field_stays_live() -> None:
    """Every configuration saved before this field existed keeps trading as it did."""
    stored = {"id": "rs-pullback-default", "name": "RS Pullback", "strategy_type": "rs-pullback-v1"}
    assert StrategyConfiguration.model_validate(stored).live_orders is True


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):  # noqa: ANN201
        return self.values.get(key)

    async def set(self, key: str, value: str, nx: bool = False, **_: object):  # noqa: ANN201
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, token: str) -> int:
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0

    async def publish(self, *_: object) -> int:
        return 0


async def _offered(monkeypatch: pytest.MonkeyPatch, strategy: StrategyConfiguration) -> tuple[list, list]:
    from app.core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://sidra:secret@postgres/sidra",
        redis_url="redis://redis:6379/0",
        jwt_secret="a-secure-test-secret-that-is-longer-than-32-characters",
        _env_file=None,
    )
    orchestrator = PaperScannerOrchestrator(settings, _Redis())  # type: ignore[arg-type]
    signal = PaperSignal(
        signal_key=uuid4().hex,
        instrument_token=TOKEN,
        session_date=SESSION,
        candle_opened_at=datetime(2026, 11, 18, 4, 50, tzinfo=UTC),
        strategy_version=f"{strategy.strategy_type}@1",
        side="LONG",
        entry_price=Decimal("105.2"),
        stop_price=Decimal("104.674"),
        target_price=Decimal("106.252"),
        quantity=10,
        risk_amount=Decimal("5"),
        score=70,
    )
    signal.id = uuid4()
    offered: list = []
    alerted: list = []
    decision = SimpleNamespace(next_state=PAPER_SIGNALLED, side="LONG", reason="", score_breakdown={})
    monkeypatch.setattr(module.StrategyRegistry, "evaluate", staticmethod(lambda *_a, **_k: decision))

    async def nothing(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return None

    async def record(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return signal

    async def reserve(_sig):  # noqa: ANN001, ANN202
        return PaperOutcome(queued=True, blocks_live=False, reason=None)

    async def alert(sig):  # noqa: ANN001, ANN202
        alerted.append(sig)

    async def offer(sig):  # noqa: ANN001, ANN202
        offered.append(sig)

    monkeypatch.setattr(orchestrator, "_signal_block_reason", nothing)
    monkeypatch.setattr(orchestrator, "_record_evaluation", nothing)
    monkeypatch.setattr(orchestrator, "_record", record)
    monkeypatch.setattr(orchestrator, "_reserve_paper", reserve)
    monkeypatch.setattr(orchestrator, "_alert", alert)
    monkeypatch.setattr(orchestrator, "_offer_live_entry", offer)
    await orchestrator._evaluate_strategy(
        candle(*AFTER_FIRST_HOUR, "104.9", "105.2"), {}, {}, {"maximum_daily_trades": 3}, strategy
    )
    return offered, alerted


async def test_a_paper_only_strategy_never_reaches_the_live_path(monkeypatch: pytest.MonkeyPatch) -> None:
    offered, alerted = await _offered(monkeypatch, configuration(live_orders=False))
    assert offered == []
    assert len(alerted) == 1  # still recorded and alerted, as paper


async def test_a_live_strategy_is_still_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    offered, _ = await _offered(
        monkeypatch, StrategyConfiguration(id="rs-pullback-default", name="RS Pullback", strategy_type="rs-pullback-v1")
    )
    assert len(offered) == 1


# --- paper-only fills stay out of the live ceilings ------------------------

# A date no other test writes to: several record rows against today.
DB_SESSION = date(2031, 3, 5)


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperFill))
        await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token == TOKEN))
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        await session.commit()


@pytest.fixture
async def clean_db():
    await _clean()
    yield
    await _clean()


async def _paper_fill(strategy_version: str) -> None:
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"cpr-{uuid4()}",
            instrument_token=TOKEN,
            session_date=DB_SESSION,
            candle_opened_at=datetime(2031, 3, 5, 4, 50, tzinfo=UTC),
            strategy_version=strategy_version,
            side="LONG",
            entry_price=Decimal("105"),
            stop_price=Decimal("104.5"),
            target_price=Decimal("106"),
            quantity=10,
            risk_amount=Decimal("5"),
            score=70,
            score_breakdown={},
            strategy_snapshot={},
            indicator_snapshot={},
        )
        session.add(signal)
        await session.flush()
        session.add(
            PaperOrder(
                paper_signal_id=signal.id,
                client_order_id=f"cpr-{uuid4()}",
                instrument_token=TOKEN,
                session_date=DB_SESSION,
                strategy_version=strategy_version,
                side="BUY",
                order_type="MARKET",
                order_role="ENTRY",
                quantity=10,
                filled_quantity=10,
                status="FILLED",
                eligible_after=datetime(2031, 3, 5, 4, 50, tzinfo=UTC),
            )
        )
        await session.commit()


async def test_a_paper_only_fill_does_not_spend_a_live_trade(clean_db) -> None:  # noqa: ANN001
    await _paper_fill(f"{CPR_BREAKOUT_VERSION}@1")
    async with SessionLocal() as session:
        everything = await count_filled_entries(session, DB_SESSION)
        for_live = await count_filled_entries(session, DB_SESSION, exclude_strategy_types=(CPR_BREAKOUT_VERSION,))
        names_for_live = await filled_entry_instruments(
            session, DB_SESSION, exclude_strategy_types=(CPR_BREAKOUT_VERSION,)
        )
    assert everything.total == 1
    assert for_live.total == 0
    assert TOKEN not in names_for_live


async def test_a_paper_only_strategy_counts_only_its_own_fills(clean_db) -> None:  # noqa: ANN001
    await _paper_fill("rs-pullback-v1@3")
    async with SessionLocal() as session:
        own = await count_filled_entries(session, DB_SESSION, strategy_type=CPR_BREAKOUT_VERSION)
        own_names = await filled_entry_instruments(session, DB_SESSION, only_strategy_type=CPR_BREAKOUT_VERSION)
        live_names = await filled_entry_instruments(session, DB_SESSION)
    assert own.total == 0
    assert own_names == ()
    assert TOKEN in live_names


async def test_a_type_with_a_live_configuration_is_never_excluded() -> None:
    """Only a type that is paper-only everywhere is left out of the live ceilings."""
    stored = [
        configuration().model_dump(),
        configuration(id="cpr-live", live_orders=True).model_dump(),
        StrategyConfiguration(id="rs", name="RS Pullback", strategy_type="rs-pullback-v1").model_dump(),
    ]

    class _Session:
        async def get(self, *_: object):  # noqa: ANN202
            return SimpleNamespace(value=stored)

    assert await StrategyRegistry.paper_only_types(_Session()) == ()
    stored.pop(1)
    assert await StrategyRegistry.paper_only_types(_Session()) == (CPR_BREAKOUT_VERSION,)
