"""Fetching what the broker says a session was worth.

The reconciliation on the History screen has been complete since it was built
except for one thing: nothing wrote the broker side, so every live day would
have read BROKER DATA PENDING forever. This is that writer, and three of its
details are the kind that fail quietly rather than loudly:

**The financial year.** Upstox requires it, India's runs April to March, and a
wrong one returns an empty report rather than an error — which looks exactly
like a day on which nothing was traded.

**Absent versus zero.** A broker that did not report a figure and a broker that
reported zero are different claims. Only one of them is worth reconciling
against, and collapsing them would turn silence into a contradiction.

**Append, never update.** A broker's own figures settle over hours. Overwriting
would destroy the evidence that a day moved from MISMATCH to MATCHED, which is
the first thing somebody wants when they see it happen.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models import BrokerDaySnapshot
from app.db.session import SessionLocal
from app.services import broker_day_figures as figures
from app.services.trade_history import MATCHED, latest_broker_snapshots

SESSION_DATE = date(2026, 9, 21)


# --- the financial year --------------------------------------------------


@pytest.mark.parametrize(
    ("session_date", "expected"),
    [
        (date(2025, 4, 1), "2526"),
        (date(2025, 12, 31), "2526"),
        (date(2026, 1, 1), "2526"),
        (date(2026, 3, 31), "2526"),
        (date(2026, 4, 1), "2627"),
        (date(2024, 4, 1), "2425"),
    ],
)
def test_the_financial_year_runs_april_to_march(session_date, expected):
    assert figures.financial_year(session_date) == expected


def test_the_january_to_march_half_is_the_one_that_gets_written_wrong():
    # A January session belongs to the year that started the previous April.
    # Reading the calendar year off the date would ask for 2627 and be handed
    # an empty report, which reads as "no trades" rather than as a mistake.
    assert figures.financial_year(date(2026, 1, 15)) != figures.financial_year(date(2026, 4, 15))


# --- reading the reports -------------------------------------------------


def test_realised_pnl_is_the_difference_between_the_matched_amounts():
    rows = [
        {"buy_amount": "1000", "sell_amount": "1200"},
        {"buy_amount": "500", "sell_amount": "400"},
    ]
    realised, turnover, count = figures.read_profit_loss(rows)
    assert realised == Decimal("100")
    assert turnover == Decimal("3100")
    assert count == 2


def test_a_row_with_neither_amount_contributes_nothing_not_zero():
    realised, turnover, count = figures.read_profit_loss([{"scrip_name": "RELIANCE"}])
    assert (realised, turnover, count) == (None, None, 0)


def test_an_empty_report_reports_nothing_rather_than_zero():
    # Zero would be a claim that the broker said the day was flat. It did not
    # say anything, and the History screen draws that distinction.
    assert figures.read_profit_loss([]) == (None, None, 0)


def test_rubbish_rows_do_not_raise():
    realised, _turnover, count = figures.read_profit_loss(["not a dict", None, {"buy_amount": "abc"}])
    assert count == 0 and realised is None


def test_charges_are_read_from_the_nested_total():
    assert figures.read_charges({"charges_breakdown": {"total": "62.40"}}) == Decimal("62.40")


def test_charges_are_added_up_when_the_broker_itemises_without_totalling():
    body = {
        "charges_breakdown": {
            "brokerage": "20",
            "taxes": {"gst": "3.6", "stt": "10", "stamp_duty": "0.4"},
            "charges": {"transaction": "1.5", "sebi_turnover": "0.1"},
        }
    }
    assert figures.read_charges(body) == Decimal("35.6")


def test_a_charges_body_with_nothing_in_it_reports_nothing():
    assert figures.read_charges({}) is None
    assert figures.read_charges({"charges_breakdown": {}}) is None


# --- the fetch, against a fake broker ------------------------------------


class FakeReportClient:
    """A stand-in for the read-only Upstox client. It cannot place an order."""

    def __init__(self, pages: list[list[dict]], charges: dict):
        self.pages = pages
        self.charges_body = charges
        self.calls: list[dict] = []

    async def trade_profit_loss(self, **kwargs):
        self.calls.append(kwargs)
        index = kwargs["page_number"] - 1
        return self.pages[index] if index < len(self.pages) else []

    async def trade_charges(self, **kwargs):
        self.calls.append(kwargs)
        return self.charges_body


async def test_the_fetch_asks_for_one_day_and_the_right_financial_year():
    client = FakeReportClient([[{"buy_amount": "100", "sell_amount": "150"}]], {"charges_breakdown": {"total": "9"}})
    result = await figures.fetch_upstox_day(client, SESSION_DATE)
    first = client.calls[0]
    assert first["from_date"] == SESSION_DATE and first["to_date"] == SESSION_DATE
    assert first["financial_year"] == "2627"  # September 2026 is FY 2026-27
    assert result.realized_pnl == Decimal("50")
    assert result.charges == Decimal("9")


async def test_a_day_reported_as_costing_nothing_is_a_day_not_yet_settled():
    """The defect this guards against was on an operator's screen.

    Upstox answers both reports before settlement: an empty P&L report, and a
    charges breakdown totalling zero. The History screen read that literally and
    told the operator "UPSTOX charged ₹0 against our estimate of ₹25.94. The
    broker's figure is the real cost." No executed equity trade in India costs
    nothing, so a zero there is a figure not yet computed.
    """
    client = FakeReportClient([[]], {"charges_breakdown": {"total": "0"}})
    result = await figures.fetch_upstox_day(client, SESSION_DATE)
    assert result.charges is None
    assert result.realized_pnl is None


async def test_a_real_charge_is_still_a_real_charge():
    """The guard must not swallow the figure it exists to wait for."""
    client = FakeReportClient(
        [[{"buy_amount": "100", "sell_amount": "150"}]], {"charges_breakdown": {"total": "25.94"}}
    )
    assert (await figures.fetch_upstox_day(client, SESSION_DATE)).charges == Decimal("25.94")


def test_zero_is_absence_and_a_negative_is_not():
    # A credit note or a refunded charge is a figure the broker did report.
    assert figures.settled_charges(Decimal("0")) is None
    assert figures.settled_charges(Decimal("0.00")) is None
    assert figures.settled_charges(None) is None
    assert figures.settled_charges(Decimal("-2.50")) == Decimal("-2.50")
    assert figures.settled_charges(Decimal("0.01")) == Decimal("0.01")


async def test_an_unsettled_day_reads_as_pending_rather_than_free():
    """End to end: the zero must not reach the operator as a reconciled cost."""
    from app.services.trade_history import BROKER_DATA_PENDING, reconcile_day

    client = FakeReportClient([[]], {"charges_breakdown": {"total": "0"}})
    day = await figures.fetch_upstox_day(client, SESSION_DATE)
    snapshot = SimpleNamespace(
        broker="UPSTOX",
        source="upstox:trade/profit-loss",
        realized_pnl=day.realized_pnl,
        charges=day.charges,
    )
    status, note = reconcile_day(
        live_trades=2, local_gross=Decimal("172.27"), local_charges=Decimal("25.94"), snapshot=snapshot
    )
    assert status == BROKER_DATA_PENDING
    assert "₹0" not in note


async def test_the_fetch_stops_paging_on_a_short_page():
    client = FakeReportClient([[{"buy_amount": "1", "sell_amount": "2"}]], {})
    await figures.fetch_upstox_day(client, SESSION_DATE)
    pages = [call for call in client.calls if "page_number" in call]
    assert len(pages) == 1


async def test_the_fetch_is_bounded_even_if_the_broker_never_shortens_a_page():
    full = [{"buy_amount": "1", "sell_amount": "2"} for _ in range(figures.PAGE_SIZE)]
    client = FakeReportClient([full] * (figures.MAX_PAGES + 5), {})
    await figures.fetch_upstox_day(client, SESSION_DATE)
    pages = [call for call in client.calls if "page_number" in call]
    assert len(pages) == figures.MAX_PAGES


async def test_the_whole_response_is_kept_for_investigating_a_disagreement():
    client = FakeReportClient([[{"buy_amount": "100", "sell_amount": "150", "scrip_name": "RELIANCE"}]], {"x": 1})
    result = await figures.fetch_upstox_day(client, SESSION_DATE)
    assert result.payload["rows"][0]["scrip_name"] == "RELIANCE"
    assert result.payload["financial_year"] == "2627"


# --- recording -----------------------------------------------------------


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(
            delete(BrokerDaySnapshot).where(
                BrokerDaySnapshot.session_date >= date(2026, 3, 1),
                BrokerDaySnapshot.session_date <= date(2026, 10, 31),
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def test_a_second_fetch_appends_rather_than_replacing():
    async with SessionLocal() as session:
        await figures.record(
            session,
            SESSION_DATE,
            figures.DayFigures(Decimal("380"), Decimal("40"), None, 2, {}),
        )
        await session.commit()
    async with SessionLocal() as session:
        await figures.record(
            session,
            SESSION_DATE,
            figures.DayFigures(Decimal("500"), Decimal("40"), None, 2, {}),
        )
        await session.commit()

    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(select(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE))
            ).all()
        )
    assert len(rows) == 2


async def test_the_history_screen_compares_against_the_newest_fetch():
    # The reason appending is safe: a day corrected by a later fetch stops
    # reading MISMATCH, and the earlier row survives as evidence it moved.
    async with SessionLocal() as session:
        early = await figures.record(
            session, SESSION_DATE, figures.DayFigures(Decimal("380"), Decimal("40"), None, 2, {})
        )
        early.fetched_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
        await session.commit()
    async with SessionLocal() as session:
        late = await figures.record(
            session, SESSION_DATE, figures.DayFigures(Decimal("500"), Decimal("40"), None, 2, {})
        )
        late.fetched_at = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)
        await session.commit()

    async with SessionLocal() as session:
        newest = (await latest_broker_snapshots(session, SESSION_DATE, SESSION_DATE))[SESSION_DATE]
    assert newest.realized_pnl == Decimal("500.0000")

    from app.services.trade_history import reconcile_day

    status, _ = reconcile_day(live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=newest)
    assert status == MATCHED


async def test_an_unreported_figure_is_stored_as_absent_not_zero():
    async with SessionLocal() as session:
        await figures.record(session, SESSION_DATE, figures.DayFigures(None, Decimal("40"), None, None, {}))
        await session.commit()
    async with SessionLocal() as session:
        row = await session.scalar(select(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE))
    assert row.realized_pnl is None
    assert row.charges == Decimal("40.0000")


# --- which session a row belongs to --------------------------------------
#
# Upstox sends dates as strings and does not say which layout. Read the wrong
# way round, "01-10-2026" is a real date in January -- a trade filed under the
# wrong month, on a screen that gives no hint anything went wrong.


@pytest.mark.parametrize(
    "value",
    ["2026-10-01", "01-10-2026", "01/10/2026", "2026/10/01", "2026-10-01T09:30:00+05:30", "2026-10-01 09:30:00"],
)
def test_a_row_date_is_read_whatever_layout_it_arrives_in(value):
    assert figures.row_date(value) == date(2026, 10, 1)


@pytest.mark.parametrize("value", [None, "", "   ", "not a date", 12345, {}])
def test_an_unreadable_row_date_is_none_rather_than_a_guess(value):
    assert figures.row_date(value) is None


def test_a_row_is_filed_under_the_day_it_closed_on():
    grouped, unfiled = figures.group_by_session(
        [
            {"buy_date": "01-10-2026", "sell_date": "01-10-2026", "sell_amount": "100"},
            {"buy_date": "05-10-2026", "sell_date": "05-10-2026", "sell_amount": "200"},
            {"buy_date": "05-10-2026", "sell_date": "05-10-2026", "sell_amount": "300"},
        ]
    )
    assert sorted(grouped) == [date(2026, 10, 1), date(2026, 10, 5)]
    assert len(grouped[date(2026, 10, 5)]) == 2
    assert unfiled == []


def test_a_carried_position_is_filed_under_the_day_the_money_was_realised():
    grouped, _ = figures.group_by_session([{"buy_date": "29-09-2026", "sell_date": "01-10-2026"}])
    assert list(grouped) == [date(2026, 10, 1)]


def test_a_row_with_no_sell_date_falls_back_to_the_buy_date():
    grouped, _ = figures.group_by_session([{"buy_date": "01-10-2026", "sell_date": None}])
    assert list(grouped) == [date(2026, 10, 1)]


def test_an_unfilable_row_is_reported_rather_than_dropped():
    """A row silently discarded is a day that quietly disagrees with the
    broker's own screen, which is the disagreement this whole path exists to
    surface."""
    grouped, unfiled = figures.group_by_session([{"scrip_name": "RELIANCE", "sell_amount": "100"}])
    assert grouped == {}
    assert len(unfiled) == 1


# --- the financial year boundary -----------------------------------------


def test_a_range_inside_one_financial_year_is_one_request():
    assert figures.financial_year_spans(date(2026, 9, 1), date(2026, 10, 6)) == [
        ("2627", date(2026, 9, 1), date(2026, 10, 6))
    ]


def test_a_range_crossing_the_april_boundary_is_split():
    """Asking with one year for both halves returns an empty report for the half
    that does not belong to it -- no error, just months that look untraded."""
    assert figures.financial_year_spans(date(2026, 3, 20), date(2026, 4, 10)) == [
        ("2526", date(2026, 3, 20), date(2026, 3, 31)),
        ("2627", date(2026, 4, 1), date(2026, 4, 10)),
    ]


def test_a_multi_year_range_is_split_into_one_span_per_year():
    spans = figures.financial_year_spans(date(2025, 1, 1), date(2026, 10, 6))
    assert [year for year, _, _ in spans] == ["2425", "2526", "2627"]


def test_a_single_day_is_a_single_span():
    assert figures.financial_year_spans(SESSION_DATE, SESSION_DATE) == [("2627", SESSION_DATE, SESSION_DATE)]


# --- the sync, which the backfill and both scheduled jobs all run ---------


class FakeRangeClient:
    """Answers a range request with whatever rows are given, by date."""

    def __init__(self, rows: list[dict], charges: dict | None = None):
        self.rows = rows
        self.charges_body = charges if charges is not None else {"charges_breakdown": {"total": "37.65"}}
        self.range_calls: list[dict] = []
        self.charge_calls: list[date] = []

    async def trade_profit_loss(self, **kwargs):
        if kwargs["page_number"] > 1:
            return []
        self.range_calls.append(kwargs)
        return self.rows

    async def trade_charges(self, **kwargs):
        self.charge_calls.append(kwargs["from_date"])
        return self.charges_body


def two_days() -> list[dict]:
    return [
        {"buy_date": "01-10-2026", "sell_date": "01-10-2026", "buy_amount": "1000", "sell_amount": "1166.93"},
        {"buy_date": "05-10-2026", "sell_date": "05-10-2026", "buy_amount": "900", "sell_amount": "880"},
    ]


async def test_the_sync_records_one_day_per_session_the_broker_reported():
    client = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        report = await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()
        stored = await latest_broker_snapshots(session, date(2026, 10, 1), date(2026, 10, 6))

    assert report.days_recorded == [date(2026, 10, 1), date(2026, 10, 5)]
    assert sorted(stored) == [date(2026, 10, 1), date(2026, 10, 5)]
    assert stored[date(2026, 10, 1)].realized_pnl == Decimal("166.9300")


async def test_the_trades_come_back_in_one_request_for_the_whole_range():
    """The shape is the cost control: a month of sessions is one request for the
    trades, and only the charges are asked for a day at a time."""
    client = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        report = await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    assert len(client.range_calls) == 1
    assert client.charge_calls == [date(2026, 10, 1), date(2026, 10, 5)]
    assert report.requests == 3


async def test_a_day_whose_charges_have_settled_is_never_asked_about_again():
    client = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    again = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        report = await figures.sync_days(session, again, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    assert again.charge_calls == []
    assert report.days_recorded == []
    assert report.days_skipped == [date(2026, 10, 1), date(2026, 10, 5)]
    assert report.requests == 1


async def test_an_unsettled_day_is_asked_about_again():
    """The whole point of the morning pass: a day fetched before settlement kept
    its realised figure and no cost, and nothing ever asked again."""
    unsettled = FakeRangeClient(two_days(), charges={"charges_breakdown": {"total": "0"}})
    async with SessionLocal() as session:
        await figures.sync_days(session, unsettled, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()
        waiting = await figures.dates_awaiting_charges(session, date(2026, 10, 1), date(2026, 10, 6))

    assert waiting == {date(2026, 10, 1), date(2026, 10, 5)}

    settled = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        report = await figures.sync_days(session, settled, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()
        stored = await latest_broker_snapshots(session, date(2026, 10, 1), date(2026, 10, 6))
        still_waiting = await figures.dates_awaiting_charges(session, date(2026, 10, 1), date(2026, 10, 6))

    assert report.days_recorded == [date(2026, 10, 1), date(2026, 10, 5)]
    assert stored[date(2026, 10, 1)].charges == Decimal("37.6500")
    assert still_waiting == set()


async def test_force_re_asks_a_settled_day():
    client = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    again = FakeRangeClient(two_days())
    async with SessionLocal() as session:
        report = await figures.sync_days(session, again, date(2026, 10, 1), date(2026, 10, 6), force=True)
        await session.commit()

    assert len(again.charge_calls) == 2
    assert report.days_skipped == []


async def test_a_row_outside_the_range_asked_for_is_not_filed():
    """A snapshot built from part of a day's trades would be worse than none:
    it reads as a settled figure and disagrees with the broker's own screen."""
    rows = two_days() + [{"buy_date": "20-09-2026", "sell_date": "20-09-2026", "buy_amount": "10", "sell_amount": "20"}]
    client = FakeRangeClient(rows)
    async with SessionLocal() as session:
        report = await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    assert date(2026, 9, 20) not in report.days_seen


async def test_a_range_with_no_trades_costs_one_request_and_records_nothing():
    client = FakeRangeClient([])
    async with SessionLocal() as session:
        report = await figures.sync_days(session, client, date(2026, 10, 1), date(2026, 10, 6))
        await session.commit()

    assert report.requests == 1
    assert report.days_recorded == []
    assert client.charge_calls == []
