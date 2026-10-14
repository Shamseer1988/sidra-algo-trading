"""The live account's caps, reported from the same arithmetic that enforces them.

The caps themselves are tested where they are enforced. What is tested here is
the half an operator actually reads: that the report says the same thing the
gate decided, that a figure it could not establish is not printed as a
comfortable zero, and that a day which ended early can be explained without a
transcript of logs -- which is what 14 October cost.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY
from app.db.models import ApplicationSetting, LiveOrderSubmission, PaperSignal, ScannerEvaluation
from app.db.session import SessionLocal
from app.services.live_usage import live_usage
from app.services.trade_counter import session_bounds_utc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import why_no_trade  # noqa: E402

DAY = date(2098, 10, 14)
OPENING = datetime(2098, 10, 14, 3, 45, tzinfo=UTC)
MIDDAY = datetime(2098, 10, 14, 5, 30, tzinfo=UTC)
TOKEN = "NSE_EQ|LU-HDFCBANK"
OTHER = "NSE_EQ|LU-IDFCFIRSTB"


class FakeAdapter:
    """Only the one method the report calls, so a broker is not implied."""

    name = "UPSTOX"

    def __init__(self, positions=None, raises: Exception | None = None) -> None:  # noqa: ANN001
        self._positions = positions if positions is not None else []
        self._raises = raises

    async def normalised_positions(self):
        if self._raises:
            raise self._raises
        return self._positions


def position(symbol: str, *, quantity="10", price="500"):  # noqa: ANN001
    return SimpleNamespace(
        symbol=symbol,
        net_quantity=Decimal(quantity) if quantity is not None else None,
        last_price=Decimal(price) if price is not None else None,
        average_price=None,
        instrument_token=None,
    )


async def save_controls(**overrides) -> None:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, TRADING_KEY)
        value = {**DEFAULT_TRADING_CONTROLS, **overrides}
        if row:
            row.value = value
        else:
            session.add(ApplicationSetting(key=TRADING_KEY, value=value))
        await session.commit()


async def signal(
    token: str = TOKEN, *, risk: str = "86.08", status: str = "ACCEPTED", detail: str | None = None
) -> PaperSignal:
    async with SessionLocal() as session:
        row = PaperSignal(
            signal_key=f"lu-{uuid4()}",
            instrument_token=token,
            session_date=DAY,
            candle_opened_at=MIDDAY,
            strategy_version="orb-retest-v1@15",
            side="LONG",
            entry_price=Decimal("1000"),
            stop_price=Decimal("992"),
            target_price=Decimal("1016"),
            quantity=10,
            risk_amount=Decimal(risk),
            score=80,
            status=status,
            alert_detail=detail,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def submission(*, signal_id=None, status: str = "ACCEPTED", planned_risk: str | None = "86.08") -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:12]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=TOKEN,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("1000"),
                status=status,
                paper_signal_id=signal_id,
                planned_risk=Decimal(planned_risk) if planned_risk is not None else None,
                created_at=MIDDAY,
            )
        )
        await session.commit()


async def evaluation(token: str = TOKEN, *, status: str = "ACCEPTED") -> None:
    async with SessionLocal() as session:
        session.add(
            ScannerEvaluation(
                evaluation_key=f"lu-{uuid4()}",
                instrument_token=token,
                session_date=DAY,
                candle_opened_at=MIDDAY,
                strategy_id="orb-retest-v1",
                strategy_name="ORB Retest",
                strategy_version=15,
                status=status,
                decision_state="SIGNALLED" if status == "ACCEPTED" else "AWAITING_BREAKOUT",
                reason="seeded",
                failed_conditions=[],
                data_quality_state="GOOD",
                candle_close=Decimal("1000"),
                candle_volume=100,
                score=80,
                side="LONG",
            )
        )
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        start, end = session_bounds_utc(DAY)
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == DAY))
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date == DAY))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def report(*, adapter=None):  # noqa: ANN001
    async with SessionLocal() as session:
        return await live_usage(session, adapter=adapter, session_date=DAY)


# --- the allocation cap ---------------------------------------------------


async def test_a_day_with_nothing_placed_reports_a_full_budget() -> None:
    """Capital 10000 at the default 4% is 400 for the day."""
    await save_controls()
    usage = await report()
    assert usage.allocation.used == Decimal("0")
    assert usage.allocation.limit == Decimal("400.00")
    assert usage.allocation.binding is False
    assert usage.trades == 0


async def test_the_allocation_reports_what_the_live_orders_declared() -> None:
    await save_controls()
    await submission(signal_id=(await signal()).id, planned_risk="86.08")
    usage = await report()
    assert usage.allocation.used == Decimal("86.08")
    assert usage.allocation.headroom == Decimal("313.92")
    assert usage.trades == 1


async def test_a_budget_reached_is_reported_as_blocking() -> None:
    """What an operator is looking for: not 'is it set' but 'is it stopping me'.

    A flat book from the broker, so the budget is the only thing that binds and
    the report names it alone rather than listing every cap it is unsure of.
    """
    await save_controls(maximum_daily_risk_percent=1.0)
    await submission(signal_id=(await signal(risk="100")).id, planned_risk="100")
    usage = await report(adapter=FakeAdapter([]))
    assert usage.allocation.limit == Decimal("100.00")
    assert usage.allocation.binding is True
    assert [cap.key for cap in usage.blocking] == ["allocation"]


# --- the book caps, and not pretending about them -------------------------


async def test_without_a_broker_the_book_caps_are_missing_not_zero() -> None:
    """A figure nobody asked for and a figure of zero are different answers.

    Printing '0 of 1' would describe room the account may not have. Flagging it
    as blocking would be a false alarm on every run made without a broker, so
    it is reported as missing: not clear, not blocking, and named.
    """
    await save_controls()
    usage = await report()
    assert usage.positions.known is False
    assert usage.positions.used is None
    assert usage.positions.binding is False
    assert [cap.key for cap in usage.not_asked] == ["positions", "exposure"]
    assert usage.blocking == ()
    assert "not asked" in usage.positions.detail


async def test_the_broker_book_fills_in_the_position_and_exposure_caps() -> None:
    await save_controls(maximum_open_positions=2, maximum_open_exposure_percent=100.0)
    usage = await report(adapter=FakeAdapter([position("HDFCBANK-EQ", quantity="10", price="500")]))
    assert usage.positions.used == Decimal("1")
    assert usage.positions.limit == Decimal("2")
    assert usage.positions.binding is False
    assert usage.exposure.used == Decimal("5000")
    # 10000 x 100% x 5 leverage
    assert usage.exposure.limit == Decimal("50000.000")
    assert usage.symbols == ("HDFCBANK-EQ",)


async def test_a_full_position_slot_is_reported_as_blocking() -> None:
    """The default is one position, which is the cap that ends most days."""
    await save_controls()
    usage = await report(adapter=FakeAdapter([position("HDFCBANK-EQ")]))
    assert usage.positions.binding is True
    assert "positions" in [cap.key for cap in usage.blocking]


async def test_a_squared_off_row_is_not_an_open_position() -> None:
    """It stays in the book all day at net quantity zero."""
    await save_controls()
    usage = await report(adapter=FakeAdapter([position("HDFCBANK-EQ", quantity="0")]))
    assert usage.positions.used == Decimal("0")
    assert usage.positions.binding is False


async def test_an_unreadable_quantity_blocks_rather_than_reading_as_missing() -> None:
    """The broker was asked and could not tell, which is a gate refusing now.

    The distinction from a broker nobody asked is the whole reason the state
    has three values: this one belongs in the blocking line.
    """
    await save_controls()
    usage = await report(adapter=FakeAdapter([position("HDFCBANK-EQ", quantity=None)]))
    assert usage.positions.known is False
    assert usage.positions.binding is True
    assert usage.not_asked == ()
    assert "HDFCBANK-EQ" in usage.positions.detail


async def test_a_broker_that_will_not_answer_is_reported_not_raised() -> None:
    """A report that throws at an operator mid-session is no report."""
    await save_controls()
    usage = await report(adapter=FakeAdapter(raises=RuntimeError("position book unavailable")))
    assert usage.positions.known is False
    assert usage.positions.binding is True
    assert "position book unavailable" in usage.positions.detail


async def test_exposure_binds_above_the_ceiling_and_not_at_it() -> None:
    """The gate refuses a count at its limit and an amount only above it.

    One shared comparison would make the report disagree with the gate by one
    position or one rupee, in the direction that reads as room.
    """
    await save_controls(maximum_open_positions=5, maximum_open_exposure_percent=10.0)
    at = await report(adapter=FakeAdapter([position("A", quantity="10", price="500")]))
    over = await report(adapter=FakeAdapter([position("A", quantity="10", price="500.01")]))
    assert at.exposure.used == at.exposure.limit == Decimal("5000.000")
    assert at.exposure.binding is False
    assert over.exposure.binding is True


# --- unreadable controls --------------------------------------------------


async def test_unreadable_controls_report_every_cap_as_blocking() -> None:
    """The gate refuses all four on this, so the report must not read as clear.

    The limit comes back zero when nothing can be loaded, and a report that
    tested 'is a limit configured' first would have said nothing was blocking
    while every live order was being turned away.
    """
    async with SessionLocal() as session:
        session.add(ApplicationSetting(key=TRADING_KEY, value={"account_capital": "not a number"}))
        await session.commit()
    usage = await report()
    assert usage.controls_readable is False
    assert [cap.key for cap in usage.blocking] == ["allocation", "positions", "exposure"]


# --- the price band -------------------------------------------------------


async def test_the_band_is_reported_as_applying_to_live_too() -> None:
    await save_controls(universe_max_share_price=1500.0)
    usage = await report()
    assert "1500" in usage.price_band
    assert "live path" in usage.price_band


async def test_no_band_says_so_rather_than_printing_zero() -> None:
    await save_controls()
    usage = await report()
    assert "no band configured" in usage.price_band


# --- what the operator reads ----------------------------------------------


async def run_script(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["why_no_trade.py", "--date", DAY.isoformat()])
    return await why_no_trade.main()


async def test_the_script_follows_each_accepted_signal_onto_the_live_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The 14 October shape: two accepted signals, one live order.

    Before this section the script said 'accepted 2' and stopped, and the
    operator went to the logs to find out why the account held one trade.
    """
    await save_controls()
    await evaluation()
    await evaluation(OTHER)
    traded = await signal()
    refused = await signal(OTHER, status="PAPER_RISK_REJECTED", detail="Maximum concurrent paper positions reached")
    await submission(signal_id=traded.id)
    assert await run_script(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "WHAT THE LIVE PATH DID WITH EACH ACCEPTED SIGNAL" in out
    assert "1 order(s) at broker" in out
    assert "no live order" in out
    assert "paper refused: Maximum concurrent paper positions reached" in out
    assert str(refused.instrument_token) in out or "IDFCFIRSTB" in out


async def test_the_script_reports_the_live_caps_and_what_binds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await save_controls(maximum_daily_risk_percent=1.0)
    await evaluation()
    await submission(signal_id=(await signal(risk="100")).id, planned_risk="100")
    assert await run_script(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "THE LIVE ACCOUNT'S OWN CAPS" in out
    assert "[FULL] Live risk allocated today" in out
    assert "Blocking the next live entry right now: Live risk allocated today" in out


async def test_the_script_says_which_two_caps_it_cannot_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """It does not call the broker, so it must not look like it did."""
    await save_controls()
    await evaluation()
    assert await run_script(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "inspect_live_day.py" in out
    assert "not established" in out
    # Missing, and said so -- not reported as a cap that is full.
    assert "Not read here, because the broker was not asked" in out
    assert "Blocking the next live entry right now" not in out


async def test_an_exit_order_is_named_as_one_rather_than_as_a_missing_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await save_controls()
    await evaluation()
    await submission(signal_id=(await signal()).id)
    await submission(signal_id=None, planned_risk=None)
    assert await run_script(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "carry no signal" in out
    assert "exits and protective stops" in out
