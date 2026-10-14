"""The broker's message is labelled by what happened, not by its column name.

``failure_message`` holds whatever the broker said, and on a successful order
that is "Accepted as 261014000054665". The script printed every one of those
under the word REFUSAL, so the 14 October report showed five submissions and
five refusals while all five had in fact been accepted. A warning that appears
on every success is a warning an operator learns to skip, which costs the real
one when it comes.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.trade_counter import session_bounds_utc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspect_live_day  # noqa: E402

DAY = date(2096, 10, 14)
AT = datetime(2096, 10, 14, 4, 9, tzinfo=UTC)


async def submission(*, status: str, message: str, name: str | None = None, code: str | None = None) -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:12]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol="HDFCBANK-EQ",
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=15,
                price=Decimal("717.85"),
                status=status,
                failure_message=message,
                failure_name=name,
                failure_code=code,
                created_at=AT,
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
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def run(capsys: pytest.CaptureFixture) -> str:
    args = type("Args", (), {"date": DAY, "no_broker": True})()
    assert await inspect_live_day.main(args) == 0
    return capsys.readouterr().out


async def test_an_accepted_order_does_not_report_a_refusal(capsys: pytest.CaptureFixture) -> None:
    await submission(status="ACCEPTED", message="Accepted as 261014000054665")
    out = await run(capsys)
    assert "broker said  Accepted as 261014000054665" in out
    assert "REFUSAL" not in out


async def test_a_recovered_order_is_an_acceptance_too(capsys: pytest.CaptureFixture) -> None:
    """An UNKNOWN that recovery proved had reached the broker did not fail."""
    await submission(status="RESOLVED_PLACED", message="Found in the order book by tag")
    out = await run(capsys)
    assert "REFUSAL" not in out


async def test_a_real_refusal_is_still_labelled_one(capsys: pytest.CaptureFixture) -> None:
    await submission(
        status="REJECTED",
        message="Invalid order price",
        name="UDAPI100038",
        code="price",
    )
    out = await run(capsys)
    assert "REFUSAL" in out
    assert "[UDAPI100038/price] Invalid order price" in out


async def test_an_unresolved_order_is_not_called_an_acceptance(capsys: pytest.CaptureFixture) -> None:
    """The outcome was never learned, so it is not one that went through."""
    await submission(status="UNKNOWN", message="placeOrder timed out")
    out = await run(capsys)
    assert "REFUSAL" in out
