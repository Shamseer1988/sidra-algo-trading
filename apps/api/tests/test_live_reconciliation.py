"""Live reconciliation: does broker state match ours, and may we trade.

The verdict gates live submission, so the tests that matter are the ones proving
it refuses. A reconciliation that returns "safe" when it should not is the single
failure that makes every later safeguard irrelevant.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.broker_adapter import FirstockAdapter, UpstoxAdapter
from app.services.firstock.orders import FirstockTransportUnknown
from app.services.live_reconciliation import (
    BLOCKING,
    REVIEW,
    LiveReconciliationReport,
    reconcile_live_execution,
)


class FakeScalars:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    """Only the calls reconciliation makes: scalars() for orders, add/flush.

    Two queries now, not one: OmsOrder rows and today's LiveOrderSubmission
    rows. They are returned in call order, which is fragile but honest — the
    alternative is parsing the query, and a fake that inspected SQL would break
    on a refactor that changed nothing real.
    """

    def __init__(
        self,
        oms_orders: list[object] | None = None,
        submissions: list[object] | None = None,
    ) -> None:
        self._results = [oms_orders or [], submissions or []]
        self._call = 0
        self.added: list[object] = []

    async def scalars(self, _query: object) -> FakeScalars:
        rows = self._results[min(self._call, len(self._results) - 1)]
        self._call += 1
        return FakeScalars(rows)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class FakeBroker:
    def __init__(self, *, orders=None, positions=None, raises: Exception | None = None) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._raises = raises

    async def order_book(self) -> list[dict]:
        if self._raises:
            raise self._raises
        return self._orders

    async def position_book(self) -> list[dict]:
        if self._raises:
            raise self._raises
        return self._positions

    # Upstox's own name for the same call, so one fake serves both adapters.
    async def positions(self) -> list[dict]:
        return await self.position_book()


def adapter_for(**kwargs) -> FirstockAdapter:
    """A real adapter over a fake broker.

    Real, because normalising the broker's own field names and status words is
    now the adapter's job, and hand-built normalised records would no longer be
    checking that the right fields are read. The session argument is only used
    for symbol translation, which reconciliation never does.
    """
    return FirstockAdapter(FakeBroker(**kwargs), None)


def oms_order(status: str, broker_order_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), status=status, broker_order_id=broker_order_id)


def kinds(report: LiveReconciliationReport) -> set[str]:
    return {finding.kind for finding in report.findings}


async def test_empty_account_is_clean_and_tradeable() -> None:
    report = await reconcile_live_execution(FakeSession(), adapter_for())
    assert report.status == "CLEAN"
    assert report.safe_to_trade is True
    assert report.findings == []


async def test_unreachable_broker_fails_closed() -> None:
    """ "Could not check" and "checked and it was wrong" must both stop trading."""
    client = adapter_for(raises=FirstockTransportUnknown("orderBook timed out"))
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.safe_to_trade is False
    assert report.status == "BLOCKED"
    assert kinds(report) == {"BROKER_UNREACHABLE"}


async def test_broker_order_we_do_not_know_about_blocks() -> None:
    """Something else is trading this account, or we lost a submission response."""
    client = adapter_for(orders=[{"orderNumber": "99999", "status": "OPEN"}])
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.safe_to_trade is False
    assert "UNTRACKED_BROKER_ORDER" in kinds(report)
    assert report.findings[0].broker_order_number == "99999"


async def test_open_position_is_unexplained_and_blocks() -> None:
    """The risk engine cannot size against a portfolio it does not know."""
    client = adapter_for(positions=[{"tradingSymbol": "IDEA-EQ", "netQuantity": "50"}])
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.safe_to_trade is False
    assert "UNEXPLAINED_POSITION" in kinds(report)


async def test_flat_position_rows_are_ignored() -> None:
    """A closed position still appears in the book with netQuantity zero."""
    client = adapter_for(positions=[{"tradingSymbol": "IDEA-EQ", "netQuantity": "0"}])
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.safe_to_trade is True


async def test_unknown_submission_blocks_until_resolved() -> None:
    session = FakeSession([oms_order("UNKNOWN", "12345")])
    client = adapter_for(orders=[{"orderNumber": "12345", "status": "OPEN"}])
    report = await reconcile_live_execution(session, client)
    assert report.safe_to_trade is False
    assert report.unknown_orders == 1
    assert "UNKNOWN_SUBMISSION" in kinds(report)


async def test_local_terminal_but_broker_still_working_blocks() -> None:
    """Our stop-loss accounting believes this order is done. It is not."""
    session = FakeSession([oms_order("FILLED", "555")])
    client = adapter_for(orders=[{"orderNumber": "555", "status": "OPEN"}])
    report = await reconcile_live_execution(session, client)
    assert report.safe_to_trade is False
    assert "STATUS_DIVERGENCE" in kinds(report)


async def test_broker_terminal_but_local_still_open_blocks() -> None:
    session = FakeSession([oms_order("ACKNOWLEDGED", "556")])
    client = adapter_for(orders=[{"orderNumber": "556", "status": "REJECTED"}])
    report = await reconcile_live_execution(session, client)
    assert report.safe_to_trade is False
    assert "STATUS_DIVERGENCE" in kinds(report)


async def test_agreeing_states_are_clean() -> None:
    session = FakeSession([oms_order("ACKNOWLEDGED", "557")])
    client = adapter_for(orders=[{"orderNumber": "557", "status": "OPEN"}])
    report = await reconcile_live_execution(session, client)
    assert report.safe_to_trade is True
    assert report.status == "CLEAN"
    assert report.external_orders == 1
    assert report.internal_orders == 1


async def test_missing_at_broker_is_review_not_blocking() -> None:
    """An intent created but not yet submitted looks exactly like this."""
    session = FakeSession([oms_order("QUEUED", "558")])
    report = await reconcile_live_execution(session, adapter_for())
    assert report.status == "REQUIRES_REVIEW"
    assert report.safe_to_trade is True
    assert kinds(report) == {"MISSING_AT_BROKER"}
    assert report.findings[0].severity == REVIEW


async def test_terminal_local_orders_absent_at_broker_are_not_flagged() -> None:
    """A cancelled order dropping out of today's book is ordinary."""
    session = FakeSession([oms_order("CANCELLED", "559")])
    report = await reconcile_live_execution(session, adapter_for())
    assert report.findings == []
    assert report.safe_to_trade is True


async def test_blank_order_numbers_are_skipped_not_treated_as_untracked() -> None:
    client = adapter_for(orders=[{"orderNumber": "", "status": "OPEN"}])
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.findings == []


async def test_unreadable_net_quantity_blocks_rather_than_reading_as_flat() -> None:
    """Zero means flat means safe. Unparseable means unknown, which is the opposite."""
    client = adapter_for(positions=[{"tradingSymbol": "X", "netQuantity": "not-a-number"}])
    report = await reconcile_live_execution(FakeSession(), client)
    assert report.safe_to_trade is False
    assert "UNREADABLE_POSITION" in kinds(report)


async def test_summary_fits_the_detail_column() -> None:
    orders = [{"orderNumber": str(index), "status": "OPEN"} for index in range(60)]
    report = await reconcile_live_execution(FakeSession(), adapter_for(orders=orders))
    assert report.safe_to_trade is False
    assert len(report.summary()) <= 255


async def test_several_blocking_findings_are_all_reported() -> None:
    """An operator should see the whole picture, not the first objection."""
    session = FakeSession([oms_order("UNKNOWN", "1")])
    client = adapter_for(
        orders=[{"orderNumber": "2", "status": "OPEN"}],
        positions=[{"tradingSymbol": "Y", "netQuantity": "10"}],
    )
    report = await reconcile_live_execution(session, client)
    # The UNKNOWN order carries a broker id the order book does not show, which is
    # a separate fact worth recording from "we never learned if it was submitted".
    assert kinds(report) == {
        "UNKNOWN_SUBMISSION",
        "UNTRACKED_BROKER_ORDER",
        "UNEXPLAINED_POSITION",
        "MISSING_AT_BROKER",
    }
    assert all(finding.severity == BLOCKING for finding in report.blocking)
    assert {finding.kind for finding in report.blocking} == {
        "UNKNOWN_SUBMISSION",
        "UNTRACKED_BROKER_ORDER",
        "UNEXPLAINED_POSITION",
    }
    assert report.safe_to_trade is False


async def test_a_status_no_map_recognises_blocks_rather_than_being_ignored() -> None:
    """The gap between "open" and "terminal" is where a missed order would sit.

    Without this finding an unrecognised status passes both branches of the
    divergence check silently, and an order nobody can classify reads as an
    order nobody needs to look at.
    """
    session = FakeSession([oms_order("FILLED", "800")])
    report = await reconcile_live_execution(
        session, adapter_for(orders=[{"orderNumber": "800", "status": "WEDNESDAY"}])
    )
    assert "UNREADABLE_ORDER_STATUS" in kinds(report)
    assert report.safe_to_trade is False


async def test_upstox_state_is_read_through_the_same_logic() -> None:
    """Same reconciliation, different field names. Nothing here knows which."""
    session = FakeSession()
    adapter = UpstoxAdapter(
        FakeBroker(
            orders=[{"order_id": "241015-1", "status": "open", "trading_symbol": "IDEA-EQ"}],
            positions=[{"trading_symbol": "IDEA-EQ", "quantity": 50}],
        )
    )
    report = await reconcile_live_execution(session, adapter)
    assert kinds(report) == {"UNTRACKED_BROKER_ORDER", "UNEXPLAINED_POSITION"}
    assert report.safe_to_trade is False


async def test_an_upstox_position_without_a_quantity_is_unreadable_not_flat() -> None:
    """A missing field must not read as flat, which is the one safe value."""
    report = await reconcile_live_execution(
        FakeSession(), UpstoxAdapter(FakeBroker(positions=[{"trading_symbol": "IDEA-EQ"}]))
    )
    assert "UNREADABLE_POSITION" in kinds(report)


@pytest.mark.parametrize("broker_status", ["OPEN", "TRIGGER_PENDING", "PENDING"])
async def test_every_working_status_counts_as_working(broker_status: str) -> None:
    session = FakeSession([oms_order("FILLED", "700")])
    client = adapter_for(orders=[{"orderNumber": "700", "status": broker_status}])
    report = await reconcile_live_execution(session, client)
    assert report.safe_to_trade is False


def test_report_checked_at_is_timezone_aware() -> None:
    report = LiveReconciliationReport(
        status="CLEAN",
        safe_to_trade=True,
        internal_orders=0,
        external_orders=0,
        unknown_orders=0,
        checked_at=datetime.now(UTC),
    )
    assert report.checked_at.tzinfo is not None


# --- our own live orders and positions --------------------------------------
#
# The first live order this system placed came back UNTRACKED_BROKER_ORDER and
# the position it opened came back UNEXPLAINED_POSITION, so the account blocked
# itself permanently after one fill and could not have placed an exit. The
# reconciler was reading OmsOrder only, and live submission writes the other
# table. These tests are that day, written down.


def submission(
    *,
    broker_order_numbers: list[str] | None = None,
    status: str = "ACCEPTED",
    symbol: str = "RVNL",
    token: str = "NSE_EQ|INE415G01027",
    side: str = "SELL",
    quantity: int = 143,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        client_order_id="sidra-abc",
        broker_order_numbers=broker_order_numbers or [],
        status=status,
        trading_symbol=symbol,
        # Carried separately from the symbol because the two are not
        # interchangeable: on Upstox the submission's symbol IS the token, and
        # the position book's is not.
        instrument_token=token,
        transaction_type=side,
        quantity=quantity,
        created_at=datetime.now(UTC),
    )


async def test_an_order_we_placed_is_not_reported_as_untracked() -> None:
    """The regression: the reconciler looked in the wrong table."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(broker_order_numbers=["260929000341027"])]),
        adapter_for(orders=[{"orderNumber": "260929000341027", "status": "COMPLETE", "tradingSymbol": "RVNL"}]),
    )
    assert "UNTRACKED_BROKER_ORDER" not in kinds(report)


async def test_a_filled_order_is_not_a_status_divergence() -> None:
    """Our submission row says ACCEPTED for ever; the broker says COMPLETE.

    That is the order working, not a disagreement. Treating the two tables
    alike would report every filled order as divergent.
    """
    report = await reconcile_live_execution(
        FakeSession([], [submission(broker_order_numbers=["1"])]),
        adapter_for(orders=[{"orderNumber": "1", "status": "COMPLETE", "tradingSymbol": "RVNL"}]),
    )
    assert "STATUS_DIVERGENCE" not in kinds(report)
    assert report.safe_to_trade is True


async def test_an_order_nobody_placed_is_still_untracked() -> None:
    """The check must keep catching what it was built for."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(broker_order_numbers=["1"])]),
        adapter_for(orders=[{"orderNumber": "999", "status": "OPEN", "tradingSymbol": "RVNL"}]),
    )
    assert "UNTRACKED_BROKER_ORDER" in kinds(report)
    assert report.safe_to_trade is False


async def test_a_submission_with_no_outcome_blocks() -> None:
    report = await reconcile_live_execution(FakeSession([], [submission(status="UNKNOWN")]), adapter_for())
    assert "UNKNOWN_SUBMISSION" in kinds(report)
    assert report.safe_to_trade is False


# --- positions ---------------------------------------------------------------


async def test_a_position_our_orders_explain_is_clean() -> None:
    """143 sold, 143 short. That is the trade, not an anomaly."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "-143"}]),
    )
    assert report.findings == []
    assert report.safe_to_trade is True


async def test_a_partial_fill_is_review_not_blocking() -> None:
    report = await reconcile_live_execution(
        FakeSession([], [submission(side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "-100"}]),
    )
    assert "PARTIAL_POSITION" in kinds(report)
    assert report.safe_to_trade is True


async def test_a_partially_filled_exit_is_still_accounted_for() -> None:
    """Sold 143 then bought 143 back. Any net between them is ours."""
    report = await reconcile_live_execution(
        FakeSession(
            [],
            [submission(side="SELL", quantity=143), submission(side="BUY", quantity=143)],
        ),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "-43"}]),
    )
    assert "UNEXPLAINED_POSITION" not in kinds(report)
    assert report.safe_to_trade is True


async def test_exposure_larger_than_we_could_have_created_blocks() -> None:
    report = await reconcile_live_execution(
        FakeSession([], [submission(side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "-500"}]),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)
    assert report.safe_to_trade is False


async def test_exposure_in_the_wrong_direction_blocks() -> None:
    report = await reconcile_live_execution(
        FakeSession([], [submission(side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "50"}]),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)
    assert report.safe_to_trade is False


async def test_a_position_in_a_symbol_we_never_traded_blocks() -> None:
    report = await reconcile_live_execution(
        FakeSession([], [submission(symbol="RVNL")]),
        adapter_for(positions=[{"tradingSymbol": "ADANIENT", "netQuantity": "-8"}]),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)
    assert report.safe_to_trade is False


async def test_a_rejected_submission_explains_no_exposure() -> None:
    """A rejected order created nothing, so it cannot account for a position."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(status="REJECTED", side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": "-143"}]),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)
    assert report.safe_to_trade is False


# --- the 30-Sep block -------------------------------------------------------
#
# A BHARTIARTL short this system opened itself came back UNEXPLAINED_POSITION
# with "at most 0 long, 0 short", and blocked all trading for the rest of the
# session. _plausible_range compared the position's symbol against the
# submission's trading_symbol. On Upstox those are "BHARTIARTL" and
# "NSE_EQ|INE397D01024", because UpstoxAdapter.describe() puts the token in the
# symbol field. They never matched, so our own order contributed nothing to the
# range and the gate concluded something else had opened the position.
#
# The gate behaved correctly on the facts it was given. The facts were wrong.


def upstox_adapter_for(positions, orders=None):
    return UpstoxAdapter(FakeBroker(orders=orders or [], positions=positions))


BHARTI_TOKEN = "NSE_EQ|INE397D01024"


async def test_a_position_we_opened_is_not_unexplained_when_upstox_renames_it() -> None:
    """The regression that stopped a live session after one fill."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(symbol=BHARTI_TOKEN, token=BHARTI_TOKEN, side="SELL", quantity=7)]),
        upstox_adapter_for([{"trading_symbol": "BHARTIARTL", "instrument_token": BHARTI_TOKEN, "quantity": -7}]),
    )
    assert "UNEXPLAINED_POSITION" not in kinds(report), report.summary()
    assert report.safe_to_trade, report.summary()


async def test_exposure_we_did_not_create_still_blocks_under_upstox_naming() -> None:
    """The safety property must survive the fix: a stranger's position still blocks."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(symbol=BHARTI_TOKEN, token=BHARTI_TOKEN, side="SELL", quantity=7)]),
        upstox_adapter_for(
            [{"trading_symbol": "RELIANCE", "instrument_token": "NSE_EQ|INE002A01018", "quantity": -50}]
        ),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)
    assert not report.safe_to_trade


async def test_more_than_we_could_have_opened_still_blocks_under_upstox_naming() -> None:
    """Matching the instrument must not stop the quantity from being checked."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(symbol=BHARTI_TOKEN, token=BHARTI_TOKEN, side="SELL", quantity=7)]),
        upstox_adapter_for([{"trading_symbol": "BHARTIARTL", "instrument_token": BHARTI_TOKEN, "quantity": -70}]),
    )
    assert "UNEXPLAINED_POSITION" in kinds(report)


async def test_a_position_book_without_a_token_still_matches_on_symbol() -> None:
    """Firstock names its orders properly; that path must keep working."""
    report = await reconcile_live_execution(
        FakeSession([], [submission(symbol="RVNL", token="NSE_EQ|INE415G01027", side="SELL", quantity=143)]),
        adapter_for(positions=[{"tradingSymbol": "RVNL", "netQuantity": -143}]),
    )
    assert "UNEXPLAINED_POSITION" not in kinds(report), report.summary()
