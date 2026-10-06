"""The trading record, and the four things it is allowed to say about the broker.

The reconciliation vocabulary is the part worth testing hardest, because each of
the four statuses is a different instruction to the operator:

  MATCHED               nothing to do
  ESTIMATED CHARGES     normal; the cost figure is ours, not the broker's
  BROKER DATA PENDING   go and fetch the broker's figures
  MISMATCH              stop and find out why before trusting the day

Collapsing any two of those loses the instruction. The one this suite guards
most carefully is the difference between a charge difference (expected — our
charges are admittedly an estimate) and a P&L difference (not expected — the
fills are not what we recorded).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import (
    BrokerDaySnapshot,
    LiveOrderSubmission,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperSignal,
    SessionHalt,
)
from app.db.session import SessionLocal
from app.services import trade_history as history

SESSION_DATE = date(2026, 9, 21)
# A second session, for the range totals. One day cannot show the difference
# between "the broker has settled this period" and "the broker has settled part
# of it", which is the distinction the overview has to carry.
NEXT_DATE = date(2026, 9, 22)


def snapshot(realized=None, charges=None, broker="UPSTOX", source="profit-loss/data"):
    return SimpleNamespace(
        realized_pnl=None if realized is None else Decimal(str(realized)),
        charges=None if charges is None else Decimal(str(charges)),
        broker=broker,
        source=source,
    )


# --- reconcile_day, in isolation -----------------------------------------


def test_a_paper_day_reports_estimated_charges():
    status, note = history.reconcile_day(
        live_trades=0, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=None
    )
    assert status == history.ESTIMATED_CHARGES
    assert "no broker side" in note


def test_a_paper_day_says_estimated_even_when_a_snapshot_exists():
    # A snapshot can exist for a day on which this system took no live trades —
    # somebody traded the account by hand. Our paper figures still have nothing
    # to reconcile against.
    status, _ = history.reconcile_day(
        live_trades=0, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(500, 40)
    )
    assert status == history.ESTIMATED_CHARGES


def test_a_live_day_with_no_snapshot_is_pending():
    status, note = history.reconcile_day(
        live_trades=2, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=None
    )
    assert status == history.BROKER_DATA_PENDING
    assert "2 live trades" in note


def test_one_live_trade_is_not_pluralised():
    _, note = history.reconcile_day(live_trades=1, local_gross=Decimal("0"), local_charges=Decimal("0"), snapshot=None)
    assert "1 live trade were" not in note
    assert "1 live trade " in note


def test_a_snapshot_that_reported_nothing_is_still_pending():
    status, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot()
    )
    assert status == history.BROKER_DATA_PENDING
    assert "neither" in note


def test_agreement_on_both_figures_is_matched():
    status, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(500, 40)
    )
    assert status == history.MATCHED
    assert "agrees" in note.lower()


def test_sub_rupee_rounding_is_not_a_discrepancy():
    status, _ = history.reconcile_day(
        live_trades=1,
        local_gross=Decimal("500.00"),
        local_charges=Decimal("40.00"),
        snapshot=snapshot("500.60", "40.40"),
    )
    assert status == history.MATCHED


def test_a_pnl_difference_is_a_mismatch():
    status, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(380, 40)
    )
    assert status == history.MISMATCH
    assert "380" in note and "500" in note


def test_a_mismatch_says_the_local_figures_were_not_touched():
    _, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(380, 40)
    )
    assert "unchanged" in note


def test_a_charge_difference_alone_is_not_a_mismatch():
    # The whole point of the ESTIMATED_CHARGES status. Our charges are an
    # estimate and say so; the broker's figure being different is news about
    # the estimate, not evidence that a trade is wrong.
    status, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(500, 62)
    )
    assert status == history.ESTIMATED_CHARGES
    assert "62" in note and "40" in note


def test_a_pnl_difference_outranks_a_charge_difference():
    status, _ = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(120, 62)
    )
    assert status == history.MISMATCH


def test_a_broker_that_reports_pnl_but_no_charges_stays_estimated():
    status, note = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(500, None)
    )
    assert status == history.ESTIMATED_CHARGES
    assert "no charges" in note


def test_a_broker_that_reports_only_charges_can_still_match():
    status, _ = history.reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snapshot(None, 40)
    )
    assert status == history.MATCHED


# --- a trade's status vs its day's ---------------------------------------


def test_a_paper_trade_never_inherits_a_pending_day():
    status, note = history.trade_reconciliation(history.PAPER, history.BROKER_DATA_PENDING, "waiting")
    assert status == history.ESTIMATED_CHARGES
    assert "Simulated" in note


def test_a_paper_trade_never_inherits_a_mismatch():
    status, _ = history.trade_reconciliation(history.PAPER, history.MISMATCH, "bad")
    assert status == history.ESTIMATED_CHARGES


def test_a_live_trade_inherits_its_days_verdict():
    assert history.trade_reconciliation(history.LIVE, history.MISMATCH, "bad") == (history.MISMATCH, "bad")
    assert history.trade_reconciliation(history.LIVE, history.MATCHED, "fine") == (history.MATCHED, "fine")


def test_every_status_has_a_label():
    """The UI reads these labels off the server rather than inventing them, so a
    status without one renders as a raw constant."""
    assert set(history.STATUS_LABELS) == {
        history.MATCHED,
        history.ESTIMATED_CHARGES,
        history.BROKER_DATA_PENDING,
        history.MISMATCH,
        history.RECONSTRUCTED,
    }


def test_reconstructed_is_not_a_verdict_reconcile_day_can_reach():
    """The other four say what a comparison found. This one says there was
    nothing to compare, which is a fact about the record rather than about the
    broker, so no amount of broker data can produce it."""
    assert history.RECONSTRUCTED not in {
        history.reconcile_day(live_trades=live, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=snap)[
            0
        ]
        for live in (0, 1)
        for snap in (None, snapshot(), snapshot(realized="500", charges="40"), snapshot(realized="100"))
    }


# --- against the database -------------------------------------------------


async def clean() -> None:
    async with SessionLocal() as session:
        dates = (SESSION_DATE, NEXT_DATE)
        await session.execute(delete(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date.in_(dates)))
        await session.execute(delete(SessionHalt).where(SessionHalt.session_date.in_(dates)))
        await session.execute(delete(PaperPosition).where(PaperPosition.session_date.in_(dates)))
        await session.execute(delete(PaperOrder).where(PaperOrder.session_date.in_(dates)))
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date.in_(dates)))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def a_trade(
    *,
    gross="500",
    charges="40",
    quantity=10,
    side="LONG",
    open_quantity=0,
    status="CLOSED",
    live=False,
    instrument="NSE_EQ|INE002A01018",
    strategy="orb-retest-v1@3",
    risk="100",
    session_date=SESSION_DATE,
    broker="UPSTOX",
):
    """One signal, its position, and optionally a live submission behind it."""
    gross_d, charges_d = Decimal(gross), Decimal(charges)
    key = uuid4().hex
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=key,
            instrument_token=instrument,
            session_date=session_date,
            candle_opened_at=datetime(2026, 9, 21, 4, 0, tzinfo=UTC),
            strategy_version=strategy,
            side=side,
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("106"),
            quantity=quantity,
            risk_amount=Decimal(risk),
            score=80,
        )
        session.add(signal)
        await session.flush()
        position = PaperPosition(
            paper_signal_id=signal.id,
            instrument_token=instrument,
            session_date=session_date,
            strategy_version=strategy,
            side=side,
            status=status,
            initial_quantity=quantity,
            open_quantity=open_quantity,
            average_entry_price=Decimal("100"),
            average_exit_price=None if open_quantity else Decimal("105"),
            stop_price=Decimal("98"),
            target_price=Decimal("106"),
            realized_pnl=gross_d,
            unrealized_pnl=Decimal("0"),
            fees_total=charges_d,
            total_pnl=gross_d - charges_d,
            opened_at=datetime(2026, 9, 21, 4, 5, tzinfo=UTC),
            closed_at=None if open_quantity else datetime(2026, 9, 21, 6, 0, tzinfo=UTC),
        )
        session.add(position)
        if live:
            session.add(
                LiveOrderSubmission(
                    client_order_id=key[:20],
                    paper_signal_id=signal.id,
                    broker=broker,
                    exchange="NSE",
                    trading_symbol="RELIANCE",
                    product="I",
                    price_type="LMT",
                    transaction_type="B",
                    quantity=quantity,
                    status="ACCEPTED",
                )
            )
        await session.commit()
        return signal.id, position.id


async def record_fills(signal_id, *, side="LONG", entry=None, exit=None, quantity=10):
    """The broker's own fills for a signal, as ``live_fills`` would have written them.

    A long enters by buying and exits by selling; a short the other way round.
    ``None`` for a price leaves that leg unpriced, which is how a half-known
    round trip is set up.
    """
    entry_side = "SELL" if side == "SHORT" else "BUY"
    exit_side = "BUY" if side == "SHORT" else "SELL"
    async with SessionLocal() as session:
        for leg, price in ((entry_side, entry), (exit_side, exit)):
            session.add(
                LiveOrderSubmission(
                    client_order_id=uuid4().hex[:20],
                    paper_signal_id=signal_id,
                    broker="UPSTOX",
                    exchange="NSE",
                    trading_symbol="RELIANCE",
                    product="I",
                    price_type="MARKET",
                    transaction_type=leg,
                    quantity=quantity,
                    status="ACCEPTED",
                    request_snapshot={"canonical": {"side": leg}},
                    filled_quantity=quantity,
                    average_fill_price=None if price is None else Decimal(str(price)),
                    broker_status="COMPLETE",
                )
            )
        await session.commit()


async def record_broker(
    realized=None,
    charges=None,
    broker="UPSTOX",
    fetched_at=None,
    session_date=SESSION_DATE,
    trade_count=None,
    rows=None,
):
    async with SessionLocal() as session:
        session.add(
            BrokerDaySnapshot(
                session_date=session_date,
                broker=broker,
                source="profit-loss/data",
                realized_pnl=None if realized is None else Decimal(str(realized)),
                charges=None if charges is None else Decimal(str(charges)),
                trade_count=trade_count,
                payload={"rows": rows or []},
                **({"fetched_at": fetched_at} if fetched_at else {}),
            )
        )
        await session.commit()


async def load(from_date=SESSION_DATE, to_date=SESSION_DATE, *, mode=None, broker=None):
    async with SessionLocal() as session:
        records = await history.load_trades(session, from_date, to_date, execution_mode=mode, broker=broker)
        days = await history.summarise_days(session, records, from_date, to_date, broker=broker, execution_mode=mode)
        return records, days, history.summarise_range(records, days, from_date, to_date)


async def test_a_closed_paper_trade_reads_back_with_its_money_split_three_ways():
    await a_trade(gross="500", charges="40")
    records, _, _ = await load()
    assert len(records) == 1
    assert (records[0].gross_pnl, records[0].charges, records[0].net_pnl) == (
        Decimal("500"),
        Decimal("40"),
        Decimal("460"),
    )


async def test_r_is_measured_on_net_not_gross():
    # 500 gross on 100 of planned risk is 5R before costs and 4.6R after. The
    # second figure is the one that pays.
    await a_trade(gross="500", charges="40", risk="100")
    records, _, _ = await load()
    assert records[0].r_multiple == Decimal("4.60")


async def test_an_open_trade_has_no_r_yet():
    await a_trade(open_quantity=10, status="OPEN")
    records, _, _ = await load()
    assert records[0].r_multiple is None
    assert records[0].is_open


async def test_a_trade_with_no_live_submission_is_paper():
    await a_trade()
    records, _, _ = await load()
    assert records[0].execution_mode == history.PAPER


async def test_a_trade_with_a_placed_live_submission_is_live():
    await a_trade(live=True)
    records, _, _ = await load()
    assert records[0].execution_mode == history.LIVE


async def test_a_day_totals_its_trades():
    await a_trade(gross="500", charges="40")
    await a_trade(gross="-200", charges="35")
    _, days, _ = await load()
    assert len(days) == 1
    day = days[0]
    assert (day.trades, day.wins, day.losses) == (2, 1, 1)
    assert day.gross_pnl == Decimal("300")
    assert day.charges == Decimal("75")
    assert day.net_pnl == Decimal("225")


async def test_an_exit_at_break_even_is_a_scratch_not_a_loss():
    # Gross zero, charges 40, so net is -40. Counting that as a loss would put a
    # loss in the record for a trade that did nothing.
    await a_trade(gross="0", charges="0")
    _, days, _ = await load()
    assert (days[0].wins, days[0].losses, days[0].scratches) == (0, 0, 1)


async def test_a_paper_day_is_estimated_charges_end_to_end():
    await a_trade()
    _, days, _ = await load()
    assert days[0].reconciliation == history.ESTIMATED_CHARGES


async def test_a_live_day_with_no_broker_figures_is_pending_end_to_end():
    await a_trade(live=True)
    _, days, _ = await load()
    assert days[0].reconciliation == history.BROKER_DATA_PENDING


async def test_a_live_day_matches_when_the_broker_agrees():
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="500", charges="40")
    _, days, _ = await load()
    assert days[0].reconciliation == history.MATCHED
    assert days[0].broker == "UPSTOX"
    assert days[0].broker_realized_pnl == Decimal("500.0000")


async def test_broker_figures_do_not_touch_the_local_row():
    # The constraint this whole table exists for. The broker says 380; our
    # position still says 500, and the screen shows both.
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="380", charges="40")
    records, days, _ = await load()
    assert records[0].gross_pnl == Decimal("500")
    assert days[0].gross_pnl == Decimal("500")
    assert days[0].broker_realized_pnl == Decimal("380.0000")
    assert days[0].reconciliation == history.MISMATCH


async def test_the_newest_snapshot_is_the_one_compared():
    # The table is append-only because a broker's figures settle over hours. An
    # early fetch that disagreed must not keep a day marked MISMATCH forever.
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="380", charges="40", fetched_at=datetime(2026, 9, 21, 10, 0, tzinfo=UTC))
    await record_broker(realized="500", charges="40", fetched_at=datetime(2026, 9, 21, 14, 0, tzinfo=UTC))
    _, days, _ = await load()
    assert days[0].reconciliation == history.MATCHED


async def test_a_paper_trade_on_a_live_day_is_not_marked_pending():
    await a_trade(live=True)
    await a_trade(live=False)
    records, days, _ = await load()
    assert days[0].reconciliation == history.BROKER_DATA_PENDING
    by_mode = {record.execution_mode: record.reconciliation for record in records}
    assert by_mode[history.LIVE] == history.BROKER_DATA_PENDING
    assert by_mode[history.PAPER] == history.ESTIMATED_CHARGES


async def test_a_halt_is_reported_against_the_day():
    await a_trade(gross="2100", charges="60")
    async with SessionLocal() as session:
        session.add(
            SessionHalt(
                session_date=SESSION_DATE, mode="PAPER", reason="DAILY_PROFIT_TARGET", session_pnl=Decimal("2040")
            )
        )
        await session.commit()
    _, days, totals = await load()
    assert "DAILY_PROFIT_TARGET" in days[0].halt_reason
    assert totals.halted_days == 1


async def test_the_range_totals_agree_with_the_days():
    await a_trade(gross="500", charges="40")
    await a_trade(gross="-200", charges="35")
    _, days, totals = await load()
    assert totals.net_pnl == sum(day.net_pnl for day in days)
    assert totals.trades == 2
    assert totals.trading_days == 1
    assert totals.profit_factor == Decimal("1.96")  # 460 won / 235 lost


async def test_profit_factor_is_absent_rather_than_zero_with_no_losses():
    await a_trade(gross="500", charges="40")
    _, _, totals = await load()
    assert totals.profit_factor is None


async def test_charges_are_shown_against_gross_profit():
    await a_trade(gross="500", charges="50")
    _, _, totals = await load()
    assert totals.charges_as_percent_of_gross == Decimal("10.00")


async def test_charges_percent_is_absent_when_the_range_lost_money():
    await a_trade(gross="-500", charges="50")
    _, _, totals = await load()
    assert totals.charges_as_percent_of_gross is None


async def test_the_trade_detail_carries_the_orders_and_fills_behind_it():
    signal_id, position_id = await a_trade()
    async with SessionLocal() as session:
        order = PaperOrder(
            paper_signal_id=signal_id,
            client_order_id=uuid4().hex,
            instrument_token="NSE_EQ|INE002A01018",
            session_date=SESSION_DATE,
            strategy_version="orb-retest-v1@3",
            side="BUY",
            order_type="LIMIT",
            order_role="ENTRY",
            status="FILLED",
            quantity=10,
            filled_quantity=10,
            average_fill_price=Decimal("100"),
            eligible_after=datetime(2026, 9, 21, 4, 5, tzinfo=UTC),
            fee_total=Decimal("20"),
        )
        session.add(order)
        await session.flush()
        session.add(
            PaperFill(
                paper_order_id=order.id,
                fill_key=uuid4().hex,
                instrument_token="NSE_EQ|INE002A01018",
                side="BUY",
                quantity=10,
                price=Decimal("100"),
                gross_value=Decimal("1000"),
                brokerage=Decimal("15"),
                stt=Decimal("1"),
                gst=Decimal("3"),
                total_fees=Decimal("20"),
                occurred_at=datetime(2026, 9, 21, 4, 6, tzinfo=UTC),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        found = await history.load_trade_detail(session, position_id)
    assert found is not None
    record, orders, fills = found
    assert record.position_id == position_id
    assert [order.order_role for order in orders] == ["ENTRY"]
    assert fills[0].brokerage == Decimal("15")
    # Itemised, so a disagreement with a broker bill points at a line.
    assert fills[0].total_fees == Decimal("20")


async def test_an_unknown_trade_is_not_found():
    async with SessionLocal() as session:
        assert await history.load_trade_detail(session, uuid4()) is None


async def test_a_day_outside_the_range_is_not_loaded():
    await a_trade()
    records, _, _ = await load(from_date=SESSION_DATE + timedelta(days=1), to_date=SESSION_DATE + timedelta(days=2))
    assert records == []


# --- whose fills a row is carrying ----------------------------------------
#
# The defect that produced this section was on an operator's screen: the P&L
# calendar said a day made ₹146.34 and the Upstox app said ₹166.93. Rows
# labelled LIVE were carrying the simulator's fill prices, because nothing had
# ever written a broker fill back. The gap between the two is slippage, and it
# was invisible for exactly as long as only one of the numbers reached a screen.


async def test_a_live_trade_reports_the_brokers_fills_not_the_models():
    signal_id, _ = await a_trade(gross="500", charges="40", live=True)
    # The model filled at 100 and 105; the broker actually got 100.40 and 104.20.
    await record_fills(signal_id, entry="100.40", exit="104.20")

    trade = (await load())[0][0]
    assert trade.price_source == history.BROKER
    assert trade.entry_price == Decimal("100.40")
    assert trade.exit_price == Decimal("104.20")
    assert trade.gross_pnl == Decimal("38.0000")


async def test_the_modelled_figure_survives_and_the_difference_is_slippage():
    """The paper record is what this system believed at the time. Overwriting it
    would leave nothing to audit, and would hide the number worth seeing."""
    signal_id, position_id = await a_trade(gross="500", charges="40", live=True)
    await record_fills(signal_id, entry="100.40", exit="104.20")

    trade = (await load())[0][0]
    assert trade.modelled_gross_pnl == Decimal("500")
    assert trade.slippage == Decimal("-462.00")

    async with SessionLocal() as session:
        position = await session.get(PaperPosition, position_id)
        assert position.realized_pnl == Decimal("500.0000")
        assert position.average_entry_price == Decimal("100.0000")


async def test_net_keeps_the_estimated_charge_against_the_brokers_gross():
    """Charges stay local permanently -- no broker reports them per trade."""
    signal_id, _ = await a_trade(gross="500", charges="40", live=True)
    await record_fills(signal_id, entry="100.40", exit="104.20")

    trade = (await load())[0][0]
    assert trade.charges == Decimal("40")
    assert trade.net_pnl == Decimal("-2.0000")


async def test_a_short_is_priced_the_right_way_round():
    signal_id, _ = await a_trade(gross="500", charges="40", live=True, side="SHORT")
    await record_fills(signal_id, side="SHORT", entry="183.63", exit="180.84")

    trade = (await load())[0][0]
    assert trade.entry_price == Decimal("183.63")
    assert trade.exit_price == Decimal("180.84")
    assert trade.gross_pnl == Decimal("27.9000")  # (183.63 - 180.84) × 10


async def test_a_live_trade_with_no_recorded_fill_keeps_the_model():
    """Absence is the normal state between the fill and the next reconciliation."""
    await a_trade(gross="500", charges="40", live=True)
    trade = (await load())[0][0]
    assert trade.price_source == history.MODEL
    assert trade.gross_pnl == Decimal("500.0000")
    assert trade.slippage is None


async def test_a_paper_trade_is_never_repriced():
    await a_trade(gross="500", charges="40", live=False)
    trade = (await load())[0][0]
    assert trade.price_source == history.MODEL
    assert trade.entry_price == Decimal("100.0000")


async def test_half_a_round_trip_falls_back_whole():
    """One leg priced by the broker and one by the model would be a third
    number, true of nothing."""
    signal_id, _ = await a_trade(gross="500", charges="40", live=True)
    await record_fills(signal_id, entry="100.40", exit=None)

    trade = (await load())[0][0]
    assert trade.price_source == history.MODEL
    assert trade.gross_pnl == Decimal("500.0000")
    assert trade.entry_price == Decimal("100.0000")


async def test_the_day_total_is_the_sum_of_the_rows_the_operator_can_see():
    """The complaint that started this: a day figure that no row explained."""
    first, _ = await a_trade(gross="500", charges="40", live=True)
    second, _ = await a_trade(gross="-200", charges="35", live=True)
    await record_fills(first, entry="100.40", exit="104.20")
    await record_fills(second, entry="100.00", exit="99.00")

    trades, days, _ = await load()
    assert days[0].gross_pnl == sum(trade.gross_pnl for trade in trades)
    assert days[0].net_pnl == sum(trade.net_pnl for trade in trades)


async def test_the_day_is_reconciled_against_the_figures_on_the_screen():
    """The verdict has to compare the broker against what the rows now show.

    Computed from the modelled gross while the rows showed the broker's, a day
    in perfect agreement would have been reported as a MISMATCH -- a stop-and-
    investigate instruction raised by the reconciliation's own blind spot.
    """
    signal_id, _ = await a_trade(gross="500", charges="40", live=True)
    await record_fills(signal_id, entry="100.40", exit="104.20")
    await record_broker(realized="38.00", charges="40")

    trade = (await load())[0][0]
    assert trade.reconciliation != history.MISMATCH


# --- one account's worth ------------------------------------------------------
#
# The P&L calendar summed paper and live together, so an operator reading "what
# did I make" got a figure their broker account had never seen: a day with one
# live trade and one paper trade showed the two added up.


async def test_the_record_can_be_narrowed_to_live_trades():
    await a_trade(gross="500", charges="40", live=True)
    await a_trade(gross="-200", charges="35", live=False)

    async with SessionLocal() as session:
        live = await history.load_trades(session, SESSION_DATE, SESSION_DATE, execution_mode="LIVE")
        paper = await history.load_trades(session, SESSION_DATE, SESSION_DATE, execution_mode="PAPER")
        both = await history.load_trades(session, SESSION_DATE, SESSION_DATE)

    assert [record.execution_mode for record in live] == ["LIVE"]
    assert [record.execution_mode for record in paper] == ["PAPER"]
    assert len(both) == 2


async def test_a_live_trade_carries_the_broker_it_reached():
    await a_trade(live=True)
    trade = (await load())[0][0]
    assert trade.broker == "UPSTOX"


async def test_a_paper_trade_has_no_broker():
    await a_trade(live=False)
    assert (await load())[0][0].broker is None


async def test_the_record_can_be_narrowed_to_one_broker():
    """Two connected brokers are two accounts, and a figure summing them would
    be true of neither."""
    await a_trade(gross="500", live=True)

    async with SessionLocal() as session:
        ours = await history.load_trades(session, SESSION_DATE, SESSION_DATE, broker="UPSTOX")
        other = await history.load_trades(session, SESSION_DATE, SESSION_DATE, broker="FIRSTOCK")

    assert len(ours) == 1
    assert other == []


async def test_the_broker_filter_ignores_case():
    await a_trade(live=True)
    async with SessionLocal() as session:
        assert len(await history.load_trades(session, SESSION_DATE, SESSION_DATE, broker="upstox")) == 1


async def test_an_unrecognised_mode_narrows_nothing():
    """A typo in a query string must not silently empty the record."""
    await a_trade(live=True)
    await a_trade(live=False)
    async with SessionLocal() as session:
        assert len(await history.load_trades(session, SESSION_DATE, SESSION_DATE, execution_mode="BOTH")) == 2


async def test_the_broker_is_compared_against_the_trades_it_actually_saw():
    """A paper trade on a live day must not look like a broker mismatch.

    The broker's figure covers what reached the broker and nothing else.
    Measured against a day that also holds a paper trade, every such day reports
    MISMATCH -- a stop-and-investigate instruction raised by arithmetic rather
    than by anything being wrong. ₹500 live and −₹200 paper summed to ₹300
    against a broker who correctly said ₹500.
    """
    await a_trade(gross="500", charges="40", live=True)
    await a_trade(gross="-200", charges="35", live=False)
    await record_broker(realized="500.00", charges="40")

    trades, days, _ = await load()
    by_mode = {trade.execution_mode: trade for trade in trades}

    assert by_mode["LIVE"].reconciliation != history.MISMATCH
    # The day's own totals still cover everything that happened; only the
    # comparison against the broker is narrowed.
    assert days[0].gross_pnl == Decimal("300.0000")


async def test_a_real_broker_disagreement_is_still_caught():
    """Narrowing the comparison must not make it blind."""
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="380.00", charges="40")

    trade = (await load())[0][0]
    assert trade.reconciliation == history.MISMATCH


# --- the day as the broker's own statement nets it ----------------------------
#
# The Broker view of the P&L calendar showed our modelled fills and our
# estimated charges under a heading that said "broker". For 1 October 2026 that
# read +₹146.34 net against the ₹129.28 Upstox showed on the same screen, with
# charges of ₹25.94 against the broker's ₹37.65 -- two figures, both labelled as
# the broker's account, neither of them the broker's.
#
# The fix is not to overwrite anything. The local figures stay exactly as
# recorded, in the database and on the screen; what changes is which one the
# Broker view *leads* with, and the net it leads with is computed here rather
# than assembled from two Decimals in a browser.


async def test_the_brokers_day_nets_its_own_charges_against_its_own_realised():
    await a_trade(gross="172.27", charges="25.94", live=True)
    await record_broker(realized="166.93", charges="37.65")

    day = (await load(broker="UPSTOX"))[1][0]
    assert day.broker_realized_pnl == Decimal("166.9300")
    assert day.broker_charges == Decimal("37.6500")
    assert day.broker_net_pnl == Decimal("129.2800")
    # Ours is untouched beside it. That is the whole point of the table.
    assert day.gross_pnl == Decimal("172.2700")
    assert day.charges == Decimal("25.9400")


async def test_a_realised_figure_with_no_charges_does_not_net():
    """Subtracting an absent cost gives a net that is wrong in the flattering
    direction every single time."""
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="500.00", charges=None)

    day = (await load(broker="UPSTOX"))[1][0]
    assert day.broker_realized_pnl == Decimal("500.0000")
    assert day.broker_net_pnl is None


async def test_charges_with_no_realised_figure_do_not_net():
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized=None, charges="37.65")

    day = (await load(broker="UPSTOX"))[1][0]
    assert day.broker_net_pnl is None


async def test_a_day_the_broker_has_not_reported_has_no_net_of_its_own():
    await a_trade(gross="500", charges="40", live=True)
    day = (await load(broker="UPSTOX"))[1][0]
    assert (day.broker, day.broker_net_pnl) == (None, None)


async def test_a_paper_only_view_borrows_no_broker_figures():
    """A broker's realised figure under a column of simulated trades reads as a
    claim that the simulation made that money."""
    await a_trade(gross="500", charges="40", live=True)
    await a_trade(gross="-200", charges="35", live=False)
    await record_broker(realized="500.00", charges="40")

    day = (await load(mode="PAPER"))[1][0]
    assert (day.broker, day.broker_realized_pnl, day.broker_net_pnl) == (None, None, None)


async def test_one_account_is_read_against_its_own_statement():
    """Two brokers on one day are two statements, and the UPSTOX one covers the
    UPSTOX trades only."""
    await a_trade(gross="500", charges="40", live=True, broker="UPSTOX")
    await a_trade(gross="900", charges="50", live=True, broker="FIRSTOCK")
    await record_broker(realized="500.00", charges="40", broker="UPSTOX")

    day = (await load(mode="LIVE", broker="UPSTOX"))[1][0]
    assert day.broker == "UPSTOX"
    assert day.broker_net_pnl == Decimal("460.0000")
    assert day.reconciliation == history.MATCHED


async def test_a_statement_is_never_borrowed_from_the_other_broker():
    """Without this, selecting Firstock showed Upstox's realised figure as
    Firstock's, and called the difference a mismatch."""
    await a_trade(gross="900", charges="50", live=True, broker="FIRSTOCK")
    await record_broker(realized="500.00", charges="40", broker="UPSTOX")

    day = (await load(mode="LIVE", broker="FIRSTOCK"))[1][0]
    assert (day.broker, day.broker_net_pnl) == (None, None)
    assert day.reconciliation == history.BROKER_DATA_PENDING


async def test_the_range_totals_only_the_days_the_broker_has_settled():
    await a_trade(gross="172.27", charges="25.94", live=True, session_date=SESSION_DATE)
    await a_trade(gross="300", charges="30", live=True, session_date=NEXT_DATE)
    await record_broker(realized="166.93", charges="37.65", session_date=SESSION_DATE)

    _, _, totals = await load(SESSION_DATE, NEXT_DATE, mode="LIVE", broker="UPSTOX")
    assert totals.broker_days == 1
    assert totals.days_pending_broker == 1
    assert totals.broker_realized_pnl == Decimal("166.9300")
    assert totals.broker_charges == Decimal("37.6500")
    assert totals.broker_net_pnl == Decimal("129.2800")


async def test_a_period_the_broker_has_not_settled_reports_nothing_not_zero():
    """₹0 reads as "the broker says the period was flat", which is the opposite
    of "the broker has not said"."""
    await a_trade(gross="500", charges="40", live=True)

    _, _, totals = await load(mode="LIVE", broker="UPSTOX")
    assert totals.broker_days == 0
    assert totals.days_pending_broker == 1
    assert (totals.broker_realized_pnl, totals.broker_charges, totals.broker_net_pnl) == (None, None, None)


async def test_a_paper_period_is_not_waiting_on_any_broker():
    await a_trade(gross="500", charges="40", live=False)

    _, _, totals = await load(mode="PAPER")
    assert (totals.broker_days, totals.days_pending_broker) == (0, 0)


# --- a day that exists only at the broker --------------------------------
#
# Days were only ever seeded from local positions, and a broker snapshot was an
# annotation on a day that already existed. After the history tables were
# purged the P&L calendar was empty even with every broker snapshot fetched
# back: sessions that really happened read as sessions that did not, which is
# the one thing a trading record must never do.


async def test_a_day_the_broker_reported_and_we_hold_no_record_of_still_appears():
    await record_broker(realized="166.93", charges="37.65", trade_count=2)

    days = (await load(mode="LIVE", broker="UPSTOX"))[1]
    assert [day.session_date for day in days] == [SESSION_DATE]
    assert days[0].reconstructed is True


async def test_a_rebuilt_day_carries_the_brokers_figures_as_its_own():
    """There is no local figure to keep beside them. The broker's report is the
    whole of what the day is."""
    await record_broker(realized="166.93", charges="37.65", trade_count=2)

    day = (await load(mode="LIVE", broker="UPSTOX"))[1][0]
    assert (day.gross_pnl, day.charges, day.net_pnl) == (Decimal("166.9300"), Decimal("37.6500"), Decimal("129.2800"))
    assert day.broker_net_pnl == Decimal("129.2800")
    assert (day.trades, day.live_trades) == (2, 2)


async def test_a_rebuilt_day_says_there_was_nothing_to_reconcile():
    """MATCHED would claim an agreement nobody tested; the other three would
    send the operator looking for a local record that is not there."""
    await record_broker(realized="166.93", charges="37.65", trade_count=2)

    day = (await load(mode="LIVE", broker="UPSTOX"))[1][0]
    assert day.reconciliation == history.RECONSTRUCTED
    assert "no local record" in day.reconciliation_note


async def test_a_rebuilt_day_without_settled_charges_says_so():
    await record_broker(realized="166.93", charges=None, trade_count=2)

    day = (await load(mode="LIVE", broker="UPSTOX"))[1][0]
    assert day.gross_pnl == Decimal("166.9300")
    assert day.broker_net_pnl is None
    assert "charges" in day.reconciliation_note


async def test_a_snapshot_that_reported_nothing_does_not_invent_a_flat_day():
    """Drawing a ₹0 session out of silence is the same error as treating ₹0
    charges as a cost, one level up."""
    await record_broker(realized=None, charges=None, trade_count=0)
    assert (await load(mode="LIVE", broker="UPSTOX"))[1] == []


async def test_the_paper_view_is_not_offered_a_broker_day():
    """Nothing reached a broker in a paper view by definition."""
    await record_broker(realized="166.93", charges="37.65", trade_count=2)
    assert (await load(mode="PAPER"))[1] == []


async def test_a_day_we_do_hold_a_record_of_is_not_rebuilt_over():
    """Our record and the broker's report are two sides of one day. Replacing
    the day wholesale would discard the side that carries the stop, the target
    and the strategy."""
    await a_trade(gross="172.27", charges="25.94", live=True)
    await record_broker(realized="166.93", charges="37.65", trade_count=2)

    day = (await load(mode="LIVE", broker="UPSTOX"))[1][0]
    assert day.reconstructed is False
    assert day.gross_pnl == Decimal("172.2700")
    assert day.broker_realized_pnl == Decimal("166.9300")


async def test_a_rebuilt_day_outside_the_range_is_not_loaded():
    await record_broker(realized="166.93", charges="37.65", session_date=NEXT_DATE)
    assert (await load(SESSION_DATE, SESSION_DATE, mode="LIVE", broker="UPSTOX"))[1] == []


async def test_rebuilt_days_total_into_the_range_like_any_other():
    await record_broker(realized="166.93", charges="37.65", trade_count=2, session_date=SESSION_DATE)
    await record_broker(realized="-20.00", charges="12.00", trade_count=1, session_date=NEXT_DATE)

    _, days, totals = await load(SESSION_DATE, NEXT_DATE, mode="LIVE", broker="UPSTOX")
    assert len(days) == 2
    assert totals.trading_days == 2
    assert totals.broker_days == 2
    assert totals.broker_net_pnl == Decimal("97.2800")
