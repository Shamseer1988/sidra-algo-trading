"""The 27 November audit of sip-momentum-v1, fix by fix.

Each block names the finding it pins: daily data from before the session only,
ranked selection at 10:10, stops that fill where the market actually traded,
a partly filled entry that cannot outlive its stop, the end-of-day paper
square-off, charges, and a backtest that squares off like paper does.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import MarketCandle, PaperOrder, PaperPosition, PaperSignal
from app.db.session import SessionLocal, engine
from app.services.backtesting import _exit_trade, _minute_volumes, _time_of_day_baseline
from app.services.exit_rules import ExitRules
from app.services.market_calculations import CompletedCandle, indicator_snapshot
from app.services.paper_execution import PaperExecutionControls, PaperOrderManager, transaction_costs
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.strategy_registry import StrategyConfiguration

IST_0915 = (3, 45)  # UTC


def candle(
    day: date,
    hour: int,
    minute: int,
    *,
    open_p: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: int = 1000,
    token: str = "NSE_EQ|INEAUDITFIX",
    seconds: int = 60,
) -> CompletedCandle:
    opened = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=seconds,
        opened_at=opened,
        closed_at=opened + timedelta(seconds=seconds),
        open=Decimal(open_p),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        tick_count=5,
    )


# --- daily data strictly before the session -----------------------------------


def test_a_daily_row_for_today_never_reaches_todays_levels_or_atr() -> None:
    today = date(2026, 11, 30)
    daily = [
        candle(today - timedelta(days=d), *IST_0915, close="100", high="101", low="99", seconds=86400)
        for d in range(20, 0, -1)
    ]
    leak = candle(today, *IST_0915, close="500", high="900", low="1", seconds=86400)
    session = [candle(today, *IST_0915)]
    clean = indicator_snapshot(session, session, opening_range_minutes=15, fast_ema_period=9,
                               slow_ema_period=21, volume_lookback=20, is_nifty=False, daily_candles=daily)
    leaked = indicator_snapshot(session, session, opening_range_minutes=15, fast_ema_period=9,
                                slow_ema_period=21, volume_lookback=20, is_nifty=False, daily_candles=[*daily, leak])
    assert leaked["prior_day"] == clean["prior_day"] and leaked["prior_day"]["close"] == 100.0
    assert leaked["daily_atr"] == clean["daily_atr"]


# --- stops fill where the market traded ---------------------------------------


def stop(side: str, price: str) -> SimpleNamespace:
    return SimpleNamespace(order_type="STOP", side=side, limit_price=None, stop_price=Decimal(price))


def test_a_stop_traded_through_from_the_safe_side_fills_at_the_stop() -> None:
    bar = candle(date(2026, 11, 30), 5, 0, open_p="100", high="100.5", low="97", close="97.5")
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 2.0) == (True, Decimal("98"))


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop() -> None:
    bar = candle(date(2026, 11, 30), 5, 0, open_p="97", high="97.5", low="96", close="96.5")
    # Open 97 is 1% under the 98 stop: inside the 2% band, so it fills -- at 97.
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 2.0) == (True, Decimal("97"))
    short = candle(date(2026, 11, 30), 5, 0, open_p="103", high="104", low="102.5", close="103.5")
    assert PaperOrderManager._fillable(stop("BUY", "102"), short, 2.0) == (True, Decimal("103"))


def test_a_gap_beyond_the_sl_limit_band_does_not_fill() -> None:
    # Open 95 is more than 2% under 98 (band edge 96.04) and never trades back.
    bar = candle(date(2026, 11, 30), 5, 0, open_p="95", high="95.5", low="94", close="95")
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 2.0) == (False, Decimal("0"))


def test_a_gap_beyond_the_band_fills_at_the_band_edge_if_price_comes_back() -> None:
    bar = candle(date(2026, 11, 30), 5, 0, open_p="95", high="96.5", low="94", close="96")
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 2.0) == (True, Decimal("96.0400"))


def test_with_the_band_off_every_triggered_stop_fills_at_the_open() -> None:
    bar = candle(date(2026, 11, 30), 5, 0, open_p="95", high="95.5", low="94", close="95")
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 0.0) == (True, Decimal("95"))


def test_an_untouched_stop_does_not_fill() -> None:
    bar = candle(date(2026, 11, 30), 5, 0, open_p="100", high="101", low="98.5", close="100")
    assert PaperOrderManager._fillable(stop("SELL", "98"), bar, 2.0) == (False, Decimal("0"))


# --- charges ------------------------------------------------------------------


def test_gst_is_charged_on_the_sebi_fee_and_ipft_is_included() -> None:
    controls = PaperExecutionControls()
    sell = transaction_costs(Decimal("100"), 300, "SELL", controls)
    value = Decimal("30000")
    exchange = value * Decimal("0.00297") / 100 + value * Decimal("0.0001") / 100
    assert sell.exchange_charge == exchange.quantize(Decimal("0.0001"))
    gst = (sell.brokerage + exchange + value * Decimal("0.0001") / 100) * Decimal("0.18")
    assert sell.gst == gst.quantize(Decimal("0.0001"))


# --- the backtest squares off and has a volume baseline ------------------------


def test_the_backtest_exits_on_the_clock_at_the_next_open() -> None:
    day = date(2026, 11, 30)
    # 14:57 .. 15:02 IST
    bars = [candle(day, 9, 27 + i, open_p=str(100 + i), high=str(100.5 + i), low=str(99.5 + i), close=str(100 + i))
            for i in range(6)]
    decision = SimpleNamespace(side="LONG", entry_price=Decimal("100"), stop_price=Decimal("90"),
                               target_price=Decimal("200"))
    price, at, reason = _exit_trade(
        decision, bars, 0, PaperExecutionControls(slippage_bps=0), entry_price=Decimal("100"),
        rules=ExitRules(square_off_time="15:00", target_rr=10),
    )
    # Due at the close of the 14:59 bar (15:00), filled at the 15:00 bar's open.
    assert reason == "TIME" and price == Decimal("103") and at == bars[3].opened_at


def test_the_backtest_baseline_uses_only_prior_sessions() -> None:
    days = [date(2026, 11, d) for d in (24, 25, 26, 27)]
    by_date = {d: [candle(d, *IST_0915, volume=100 * (i + 1))] for i, d in enumerate(days)}
    baseline = _time_of_day_baseline(_minute_volumes(by_date), days[3], 2)
    assert baseline == {"09:15": 250.0}  # the 25th (200) and 26th (300), not the 27th


# --- ranked selection at 10:10 -------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def zadd(self, key, mapping):  # noqa: ANN001, ANN201
        self.sets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key, start, end):  # noqa: ANN001, ANN201
        items = sorted(self.sets.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0]))
        return [member for member, _ in items]

    async def hset(self, key, field, value):  # noqa: ANN001, ANN201
        self.hashes.setdefault(key, {})[field] = value

    async def hget(self, key, field):  # noqa: ANN001, ANN201
        return self.hashes.get(key, {}).get(field)

    async def expire(self, key, seconds):  # noqa: ANN001, ANN201
        return True


async def test_only_the_top_three_by_rank_are_selected_whatever_order_they_arrived_in() -> None:
    redis = FakeRedis()
    scanner = PaperScannerOrchestrator(get_settings(), redis)
    strategy = StrategyConfiguration(id="sip-rank-test", name="SIP rank test", strategy_type="sip-momentum-v1",
                                     live_orders=False, max_trades_per_day=3)
    day = date(2026, 11, 30)
    # Arrival order is the reverse of rank order: first-come would take the three weakest.
    for token, rank in (("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 9.0), ("E", 7.0)):
        await scanner._sip_register(candle(day, 4, 39, token=token), strategy,
                                    {"rank": rank, "session_rvol": 3.0, "move_percent": 1.5, "side": "LONG"})
    chosen = {}
    for token in "ABCDE":
        chosen[token] = await scanner._sip_selection(candle(day, 4, 40, token=token), strategy)
    assert {t for t, sel in chosen.items() if sel["sip_selected"]} == {"D", "E", "C"}
    assert chosen["D"]["sip_position"] == 1 and chosen["A"]["sip_position"] == 5
    assert chosen["A"]["sip_candidates"] == 5 and chosen["D"]["sip_candidate"]["rank"] == 9.0


# --- partial entry, then a stop: the rest of the entry is cancelled -------------

RACE_DAY = date(2031, 2, 3)
RACE_TOKEN = "NSE_EQ|INERACETEST"


async def test_a_partly_filled_entry_is_cancelled_when_its_stop_closes_the_position() -> None:
    signal = PaperSignal(
        signal_key="audit-partial-entry-race", instrument_token=RACE_TOKEN, session_date=RACE_DAY,
        candle_opened_at=datetime(2031, 2, 3, 4, 0, tzinfo=UTC), strategy_version="sip-momentum-v1@1",
        side="LONG", entry_price=Decimal("100"), stop_price=Decimal("98"), target_price=Decimal("120"),
        quantity=10, risk_amount=Decimal("20"), score=0, score_breakdown={}, strategy_snapshot={},
        indicator_snapshot={},
    )
    async with SessionLocal() as session:
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
    manager = PaperOrderManager()
    try:
        await manager.queue_signal(signal)
        # 10% participation of 50 shares: the entry fills 5 of 10.
        await manager.process_completed_candle(candle(RACE_DAY, 4, 1, volume=50, token=RACE_TOKEN))
        # Next candle trades through the stop with plenty of volume.
        await manager.process_completed_candle(
            candle(RACE_DAY, 4, 2, open_p="99", high="99", low="97", close="97.5", volume=5000, token=RACE_TOKEN)
        )
        async with SessionLocal() as session:
            position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
            orders = {
                o.order_role: o
                for o in (await session.scalars(select(PaperOrder).where(PaperOrder.paper_signal_id == signal.id)))
            }
        assert position.status == "CLOSED" and position.open_quantity == 0
        assert orders["STOP"].status == "FILLED"
        assert orders["ENTRY"].status == "CANCELLED" and orders["ENTRY"].filled_quantity == 5
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperSignal).where(PaperSignal.signal_key == signal.signal_key))
            await session.commit()
        await engine.dispose()


# --- end-of-day square-off ------------------------------------------------------

EOD_DAY = date(2031, 2, 4)
EOD_TOKEN = "NSE_EQ|INEEODTEST"


async def test_a_position_with_no_candle_after_3_pm_is_closed_at_the_days_last_price() -> None:
    signal = PaperSignal(
        signal_key="audit-eod-square-off", instrument_token=EOD_TOKEN, session_date=EOD_DAY,
        candle_opened_at=datetime(2031, 2, 4, 4, 0, tzinfo=UTC), strategy_version="sip-momentum-v1@1",
        side="LONG", entry_price=Decimal("100"), stop_price=Decimal("90"), target_price=Decimal("150"),
        quantity=10, risk_amount=Decimal("100"), score=0, score_breakdown={}, strategy_snapshot={},
        indicator_snapshot={},
    )
    async with SessionLocal() as session:
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
    manager = PaperOrderManager()
    last = candle(EOD_DAY, 9, 20, close="104", token=EOD_TOKEN)  # 14:50 IST, the day's last candle
    try:
        await manager.queue_signal(signal)
        await manager.process_completed_candle(candle(EOD_DAY, 4, 1, volume=5000, token=EOD_TOKEN))
        async with SessionLocal() as session:
            session.add(MarketCandle(
                instrument_token=EOD_TOKEN, timeframe_seconds=60, session_date=EOD_DAY,
                opened_at=last.opened_at, closed_at=last.closed_at, open=last.open, high=last.high,
                low=last.low, close=last.close, volume=last.volume, tick_count=5,
            ))
            await session.commit()
        assert await manager.square_off_session(EOD_DAY) == 1
        assert await manager.square_off_session(EOD_DAY) == 0  # once only
        async with SessionLocal() as session:
            position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
            orders = list(await session.scalars(select(PaperOrder).where(PaperOrder.paper_signal_id == signal.id)))
        assert position.status == "CLOSED" and position.open_quantity == 0
        eod = next(o for o in orders if o.client_order_id.endswith(":eod"))
        assert eod.status == "FILLED" and eod.simulation_snapshot["source"] == "end_of_day_square_off"
        assert all(o.status in {"FILLED", "CANCELLED"} for o in orders)
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperSignal).where(PaperSignal.signal_key == signal.signal_key))
            await session.execute(delete(MarketCandle).where(MarketCandle.instrument_token == EOD_TOKEN))
            await session.commit()
        await engine.dispose()
