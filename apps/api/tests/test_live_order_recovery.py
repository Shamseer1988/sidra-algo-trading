"""Resolving a submission whose outcome was never learned.

The asymmetry that shapes every test here: wrongly concluding an order was never
placed invites a duplicate live order, while escalating to a person costs a
minute. So the module is allowed to say "found" and allowed to say "a human must
look", and is never allowed to say "it was not placed".
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.firstock.orders import FirstockTransportUnknown
from app.services.live_order_recovery import (
    MAX_RESOLUTION_ATTEMPTS,
    NEEDS_REVIEW,
    RESOLVED_PLACED,
    UNKNOWN,
    match_submission,
    resolve_submission,
)


class FakeClient:
    def __init__(self, book=None, raises: Exception | None = None) -> None:
        self._book = book if book is not None else []
        self._raises = raises
        self.calls = 0

    async def order_book(self) -> list[dict]:
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._book


def submission(status: str = UNKNOWN, client_order_id: str = "sidra-1", attempts: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        client_order_id=client_order_id,
        status=status,
        resolution_attempts=attempts,
        resolution_detail=None,
        broker_order_numbers=[],
        resolved_at=None,
    )


# --- the pure match -------------------------------------------------------


def test_our_identifier_in_the_book_resolves_the_submission() -> None:
    book = [{"orderNumber": "999", "remarks": "sidra-1", "status": "OPEN"}]
    result = match_submission(book, "sidra-1")
    assert result.status == RESOLVED_PLACED
    assert result.broker_order_numbers == ["999"]


def test_every_slice_of_a_resolved_submission_is_captured() -> None:
    book = [
        {"orderNumber": "1", "remarks": "sidra-1"},
        {"orderNumber": "2", "remarks": "sidra-1"},
        {"orderNumber": "3", "remarks": "someone-else"},
    ]
    assert match_submission(book, "sidra-1").broker_order_numbers == ["1", "2"]


def test_an_empty_book_is_not_yet_an_answer() -> None:
    """Absence is not proof; the book can lag."""
    assert match_submission([], "sidra-1").status == UNKNOWN


def test_a_book_without_our_order_is_not_yet_an_answer() -> None:
    result = match_submission([{"orderNumber": "5", "remarks": "other"}], "sidra-1")
    assert result.status == UNKNOWN


def test_a_book_that_carries_no_remarks_escalates_rather_than_guessing() -> None:
    """Matching on symbol and quantity cannot tell our order from a similar one."""
    book = [{"orderNumber": "5", "status": "OPEN", "tradingSymbol": "IDEA-EQ"}]
    result = match_submission(book, "sidra-1")
    assert result.status == NEEDS_REVIEW
    assert "remarks" in result.detail


def test_a_submission_without_an_identifier_cannot_be_searched_for() -> None:
    assert match_submission([{"orderNumber": "1", "remarks": "x"}], "").status == NEEDS_REVIEW


@pytest.mark.parametrize("key", ["remarks", "remark", "Remarks"])
def test_the_identifier_is_read_under_any_documented_spelling(key: str) -> None:
    assert match_submission([{"orderNumber": "1", key: "sidra-1"}], "sidra-1").status == RESOLVED_PLACED


def test_no_input_ever_produces_a_not_placed_verdict() -> None:
    """The verdict that would invite a duplicate order does not exist."""
    books = [
        [],
        [{"orderNumber": "1", "remarks": "other"}],
        [{"orderNumber": "1"}],
        [{"remarks": "sidra-1"}],
    ]
    for book in books:
        assert match_submission(book, "sidra-1").status in {UNKNOWN, NEEDS_REVIEW}


# --- the stateful resolution ---------------------------------------------


async def test_resolution_records_the_broker_numbers_and_closes_the_submission() -> None:
    record = submission()
    result = await resolve_submission(FakeClient([{"orderNumber": "77", "remarks": "sidra-1"}]), record)
    assert result.status == RESOLVED_PLACED
    assert record.status == RESOLVED_PLACED
    assert record.broker_order_numbers == ["77"]
    assert record.resolved_at is not None


async def test_a_submission_that_is_not_unknown_is_left_alone() -> None:
    record = submission(status="ACCEPTED")
    client = FakeClient()
    result = await resolve_submission(client, record)
    assert client.calls == 0
    assert result.detail == "Submission is not unknown; nothing to resolve."


async def test_repeated_absence_escalates_to_a_person() -> None:
    record = submission()
    for _ in range(MAX_RESOLUTION_ATTEMPTS):
        result = await resolve_submission(FakeClient([]), record)
    assert result.status == NEEDS_REVIEW
    assert record.status == NEEDS_REVIEW
    assert "not proof it was never placed" in record.resolution_detail


async def test_an_unreadable_order_book_also_escalates_eventually() -> None:
    record = submission()
    for _ in range(MAX_RESOLUTION_ATTEMPTS):
        await resolve_submission(FakeClient(raises=FirstockTransportUnknown("orderBook timed out")), record)
    assert record.status == NEEDS_REVIEW


async def test_a_single_broker_failure_does_not_escalate_immediately() -> None:
    record = submission()
    result = await resolve_submission(FakeClient(raises=FirstockTransportUnknown("timeout")), record)
    assert result.status == UNKNOWN
    assert record.status == UNKNOWN


async def test_a_book_without_remarks_escalates_on_the_first_attempt() -> None:
    """There is no point retrying something that cannot work."""
    record = submission()
    result = await resolve_submission(FakeClient([{"orderNumber": "1", "status": "OPEN"}]), record)
    assert result.status == NEEDS_REVIEW
    assert record.resolution_attempts == 1
