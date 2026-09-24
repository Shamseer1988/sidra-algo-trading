"""The History endpoints, including the two exports.

The exports get real tests rather than a smoke check because a spreadsheet is
where this record goes to be read by somebody who never saw the screen. Three
things have to survive the trip: the three money columns stay three columns, the
numbers arrive as numbers rather than text, and the sheet says on its face that
charges are estimated.
"""

import csv
import io
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.api.routes import history as routes
from app.db.session import SessionLocal
from app.services import trade_history
from tests.test_trade_history import SESSION_DATE, a_trade, clean, record_broker

OPERATOR = object()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def test_the_overview_reports_the_range_it_was_asked_for():
    await a_trade(gross="500", charges="40")
    async with SessionLocal() as session:
        result = await routes.overview(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    assert result.from_date == SESSION_DATE.isoformat()
    assert result.net_pnl == 460
    assert result.trades == 1


async def test_the_overview_ships_the_status_labels_so_the_ui_does_not_invent_them():
    async with SessionLocal() as session:
        result = await routes.overview(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    assert result.reconciliation_labels == trade_history.STATUS_LABELS


async def test_the_daily_list_carries_a_label_beside_the_status():
    await a_trade(live=True)
    async with SessionLocal() as session:
        rows = await routes.daily(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    assert rows[0].reconciliation == trade_history.BROKER_DATA_PENDING
    assert rows[0].reconciliation_label == "Broker data pending"


async def test_the_trade_list_can_be_pinned_to_one_session():
    await a_trade()
    async with SessionLocal() as session:
        rows = await routes.trades(session, OPERATOR, None, None, SESSION_DATE, None, None)
    assert len(rows) == 1
    assert rows[0].session_date == SESSION_DATE.isoformat()


async def test_the_trade_list_filters_by_instrument():
    await a_trade(instrument="NSE_EQ|INE002A01018")
    await a_trade(instrument="NSE_EQ|INE467B01029")
    async with SessionLocal() as session:
        rows = await routes.trades(session, OPERATOR, None, None, SESSION_DATE, "NSE_EQ|INE467B01029", None)
    assert [row.instrument_token for row in rows] == ["NSE_EQ|INE467B01029"]


async def test_the_trade_list_filters_by_strategy():
    await a_trade(strategy="orb-retest-v1@3")
    await a_trade(strategy="vwap-pullback-v1@1")
    async with SessionLocal() as session:
        rows = await routes.trades(session, OPERATOR, None, None, SESSION_DATE, None, "vwap-pullback-v1@1")
    assert [row.strategy_version for row in rows] == ["vwap-pullback-v1@1"]


async def test_an_unknown_trade_is_a_404():
    from uuid import uuid4

    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as raised:
            await routes.trade_detail(session, OPERATOR, uuid4())
    assert raised.value.status_code == 404


async def test_a_backwards_range_is_refused():
    with pytest.raises(HTTPException) as raised:
        routes._range(SESSION_DATE, SESSION_DATE - timedelta(days=1))
    assert raised.value.status_code == 400


async def test_an_enormous_range_is_refused_with_a_way_out():
    with pytest.raises(HTTPException) as raised:
        routes._range(date(2020, 1, 1), date(2026, 1, 1))
    assert "export" in raised.value.detail


async def test_the_default_range_ends_today_and_looks_back_a_month():
    begin, end = routes._range(None, None)
    assert end == routes._today()
    assert (end - begin).days == routes.DEFAULT_WINDOW_DAYS


# --- exports --------------------------------------------------------------


async def _csv_rows():
    async with SessionLocal() as session:
        response = await routes.export_csv(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    # StreamingResponse yields whatever the iterator gave it, which for the CSV
    # path is str and for the workbook path is bytes.
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
    return list(csv.reader(io.StringIO(body)))


async def test_the_csv_keeps_gross_charges_and_net_as_three_columns():
    await a_trade(gross="500", charges="40")
    rows = await _csv_rows()
    header = rows[0]
    assert {"Gross P&L", "Charges", "Net P&L"} <= set(header)
    values = dict(zip(header, rows[1], strict=True))
    assert (values["Gross P&L"], values["Charges"], values["Net P&L"]) == ("500.0", "40.0", "460.0")


async def test_the_csv_names_the_file_after_its_range():
    async with SessionLocal() as session:
        response = await routes.export_csv(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    assert f"trade-history-{SESSION_DATE}-to-{SESSION_DATE}.csv" in response.headers["Content-Disposition"]


async def _workbook():
    from openpyxl import load_workbook

    async with SessionLocal() as session:
        response = await routes.export_xlsx(session, OPERATOR, SESSION_DATE, SESSION_DATE)
    body = b"".join([chunk async for chunk in response.body_iterator])
    return load_workbook(io.BytesIO(body))


async def test_the_workbook_has_a_summary_a_day_sheet_and_a_trade_sheet():
    await a_trade()
    book = await _workbook()
    assert book.sheetnames == ["Summary", "Days", "Trades"]


async def test_the_workbook_says_on_its_face_that_charges_are_estimated():
    # The sheet outlives the screen. Somebody will open it in six months with
    # no idea that the cost column is a local model.
    await a_trade()
    book = await _workbook()
    text = " ".join(str(cell.value) for row in book["Summary"].iter_rows() for cell in row if cell.value)
    assert "estimate" in text
    assert "never per trade" in text


async def test_the_workbook_writes_money_as_numbers_not_text():
    # openpyxl writes a Decimal as a string, and a column of right-aligned text
    # that looks like money is the kind of thing nobody notices until they sum it.
    await a_trade(gross="500", charges="40")
    book = await _workbook()
    sheet = book["Trades"]
    header = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    net = dict(zip(header, row, strict=True))["Net P&L"]
    assert isinstance(net, int | float)
    assert net == 460.0


async def test_the_workbook_carries_the_broker_figures_beside_our_own():
    await a_trade(gross="500", charges="40", live=True)
    await record_broker(realized="380", charges="40")
    book = await _workbook()
    sheet = book["Days"]
    values = dict(zip([cell.value for cell in sheet[1]], [cell.value for cell in sheet[2]], strict=True))
    assert values["Gross P&L"] == 500.0
    assert values["Broker realised"] == 380.0
    assert values["Reconciliation"] == "Mismatch"
