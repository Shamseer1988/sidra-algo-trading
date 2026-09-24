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
    assert set(history.STATUS_LABELS) == {
        history.MATCHED,
        history.ESTIMATED_CHARGES,
        history.BROKER_DATA_PENDING,
        history.MISMATCH,
    }


# --- against the database -------------------------------------------------


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE))
        await session.execute(delete(SessionHalt).where(SessionHalt.session_date == SESSION_DATE))
        await session.execute(delete(PaperPosition).where(PaperPosition.session_date == SESSION_DATE))
        await session.execute(delete(PaperOrder).where(PaperOrder.session_date == SESSION_DATE))
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date == SESSION_DATE))
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
                    broker="UPSTOX",
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


async def record_broker(realized=None, charges=None, broker="UPSTOX", fetched_at=None):
    async with SessionLocal() as session:
        session.add(
            BrokerDaySnapshot(
                session_date=SESSION_DATE,
                broker=broker,
                source="profit-loss/data",
                realized_pnl=None if realized is None else Decimal(str(realized)),
                charges=None if charges is None else Decimal(str(charges)),
                payload={},
                **({"fetched_at": fetched_at} if fetched_at else {}),
            )
        )
        await session.commit()


async def load(from_date=SESSION_DATE, to_date=SESSION_DATE):
    async with SessionLocal() as session:
        records = await history.load_trades(session, from_date, to_date)
        days = await history.summarise_days(session, records, from_date, to_date)
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
