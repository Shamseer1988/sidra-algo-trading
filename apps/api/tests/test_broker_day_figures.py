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
        await session.execute(delete(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE))
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
                await session.scalars(
                    select(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE)
                )
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

    status, _ = reconcile_day(
        live_trades=1, local_gross=Decimal("500"), local_charges=Decimal("40"), snapshot=newest
    )
    assert status == MATCHED


async def test_an_unreported_figure_is_stored_as_absent_not_zero():
    async with SessionLocal() as session:
        await figures.record(session, SESSION_DATE, figures.DayFigures(None, Decimal("40"), None, None, {}))
        await session.commit()
    async with SessionLocal() as session:
        row = await session.scalar(
            select(BrokerDaySnapshot).where(BrokerDaySnapshot.session_date == SESSION_DATE)
        )
    assert row.realized_pnl is None
    assert row.charges == Decimal("40.0000")
