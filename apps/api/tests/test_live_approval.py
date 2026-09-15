"""Per-order operator approval over Telegram.

A human in the loop is only a safeguard if the loop is built correctly. The
three failures these tests are written against are: an approval that authorises
a submission made later under different conditions, an approval that can be
spent twice, and an approval that silently does nothing while the operator
believes an order was sent.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models import LiveActivation, LiveOrderApproval, LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.live_approval import (
    APPROVED,
    BLOCKED,
    EXPIRED,
    PENDING,
    REJECTED,
    SUBMITTED,
    approval_message,
    decide_live_approval,
    expire_stale_approvals,
    new_reference_id,
    request_live_approval,
)
from app.services.live_orders import LiveOrderRequest
from app.services.telegram import TelegramError

REQUEST = LiveOrderRequest(
    exchange="NSE",
    trading_symbol="IDEA-EQ",
    product="I",
    price_type="LMT",
    transaction_type="B",
    quantity=10,
    price=Decimal("418"),
)


class FakeRedis:
    async def hgetall(self, _key: str) -> dict:
        return {}


class FakeClient:
    def __init__(self) -> None:
        self.placements: list[dict] = []

    async def order_margin(self, **_kwargs: object) -> dict:
        return {"availableMargin": "500000", "marginOnNewOrder": "4180"}

    async def place_order(self, **kwargs: object) -> object:
        self.placements.append(dict(kwargs))
        return {"orderNumber": "24091500042"}


def settings(expiry: int = 180) -> SimpleNamespace:
    return SimpleNamespace(live_approval_expiry_seconds=expiry)


def readiness(ready: bool):
    async def _inspect(_session: object, _settings: object) -> SimpleNamespace:
        return SimpleNamespace(overall_ready=ready, gates=[SimpleNamespace(key="runtime_mode", passed=ready)])

    return _inspect


def patch_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(True))

    async def _recon_check(_session: object):
        from app.services.live_risk import LiveRiskCheck

        return LiveRiskCheck("reconciliation", True, "clean")

    monkeypatch.setattr("app.services.live_risk._reconciliation_check", _recon_check)


def patch_telegram(monkeypatch: pytest.MonkeyPatch, sent: list, raises: Exception | None = None) -> None:
    async def _send(_self, text, markup=None, parse_mode=None):  # noqa: ANN001
        if raises:
            raise raises
        sent.append((text, markup))
        return {"ok": True}

    monkeypatch.setattr("app.services.telegram.TelegramNotificationService.send_message", _send)


async def clean(session) -> None:  # noqa: ANN001
    await session.execute(delete(LiveOrderSubmission))
    await session.execute(delete(LiveOrderApproval))
    await session.execute(delete(LiveActivation))
    await session.commit()


async def arm(session, minutes: int = 60) -> None:  # noqa: ANN001
    session.add(
        LiveActivation(reason="test", gate_snapshot={}, expires_at=datetime.now(UTC) + timedelta(minutes=minutes))
    )
    await session.commit()


async def pending_approval(session, *, expires_in: int = 180) -> LiveOrderApproval:  # noqa: ANN001
    approval = LiveOrderApproval(
        reference_id=new_reference_id(),
        instrument_token="NSE_EQ|INE669E01016",
        trading_symbol=REQUEST.trading_symbol,
        exchange=REQUEST.exchange,
        product=REQUEST.product,
        price_type=REQUEST.price_type,
        transaction_type=REQUEST.transaction_type,
        quantity=REQUEST.quantity,
        price=REQUEST.price,
        status=PENDING,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return approval


# --- the request ----------------------------------------------------------


async def test_the_operator_is_told_what_they_are_approving(monkeypatch: pytest.MonkeyPatch) -> None:
    """A button with only a symbol on it trains people to tap yes."""
    sent: list = []
    patch_telegram(monkeypatch, sent)
    async with SessionLocal() as session:
        await clean(session)
        approval = await request_live_approval(
            session, settings(), request=REQUEST, instrument_token="NSE_EQ|INE669E01016"
        )
        text = sent[0][0]
        assert "IDEA-EQ" in text
        assert "BUY" in text
        assert "10" in text
        assert "4180" in text  # notional
        assert "real money" in text
        assert approval.status == PENDING
        await clean(session)


async def test_the_approval_is_stored_before_the_message_goes_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply must not arrive referencing an approval we have not stored."""
    stored_at_send_time: list = []

    async def _send(_self, text, markup=None, parse_mode=None):  # noqa: ANN001
        async with SessionLocal() as probe:
            rows = list((await probe.scalars(select(LiveOrderApproval))).all())
            stored_at_send_time.append(len(rows))
        return {"ok": True}

    monkeypatch.setattr("app.services.telegram.TelegramNotificationService.send_message", _send)
    async with SessionLocal() as session:
        await clean(session)
        await request_live_approval(session, settings(), request=REQUEST, instrument_token="token")
        await clean(session)
    assert stored_at_send_time == [1]


async def test_an_undeliverable_alert_blocks_rather_than_looking_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending row nobody was asked about reads as an operator who said nothing."""
    patch_telegram(monkeypatch, [], raises=TelegramError("bot token rejected"))
    async with SessionLocal() as session:
        await clean(session)
        approval = await request_live_approval(session, settings(), request=REQUEST, instrument_token="token")
        assert approval.status == BLOCKED
        assert "Telegram alert failed" in approval.block_reason
        await clean(session)


# --- the decision ---------------------------------------------------------


async def decide(session, client, reference_id: str, action: str, mode: str = "TELEGRAM_APPROVAL"):  # noqa: ANN001
    return await decide_live_approval(
        session,
        settings(),
        client,
        FakeRedis(),
        reference_id=reference_id,
        action=action,
        decided_by="12345",
        approval_mode=mode,
    )


async def test_approval_revalidates_and_then_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session)
        result = await decide(session, client, approval.reference_id, "approve")
        assert result.status == SUBMITTED
        assert len(client.placements) == 1
        stored = await session.scalar(
            select(LiveOrderApproval).where(LiveOrderApproval.reference_id == approval.reference_id)
        )
        assert stored.status == SUBMITTED
        assert stored.revalidation_snapshot["authorized"] is True
        await clean(session)


async def test_rejection_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session)
        result = await decide(session, client, approval.reference_id, "reject")
        assert result.status == REJECTED
        assert client.placements == []
        await clean(session)


async def test_an_approval_cannot_be_spent_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A double tap, or a retried webhook delivery, must produce one order."""
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session)
        first = await decide(session, client, approval.reference_id, "approve")
        second = await decide(session, client, approval.reference_id, "approve")
        assert first.status == SUBMITTED
        assert second.status == SUBMITTED
        assert "Already" in second.detail
        assert len(client.placements) == 1
        await clean(session)


async def test_an_expired_approval_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The price it was requested at has stopped being the price."""
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session, expires_in=-1)
        result = await decide(session, client, approval.reference_id, "approve")
        assert result.status == EXPIRED
        assert client.placements == []
        await clean(session)


async def test_an_approval_that_fails_revalidation_sends_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conditions moved between the ask and the answer. That is the whole point."""
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        # Deliberately not armed: the operator says yes, the system still refuses.
        approval = await pending_approval(session)
        result = await decide(session, client, approval.reference_id, "approve")
        assert result.status == BLOCKED
        assert "blocked on revalidation" in result.detail
        assert client.placements == []
        stored = await session.scalar(
            select(LiveOrderApproval).where(LiveOrderApproval.reference_id == approval.reference_id)
        )
        assert stored.status == BLOCKED
        await clean(session)


async def test_an_unknown_reference_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        result = await decide(session, client, "la-does-not-exist", "approve")
        assert result.status == BLOCKED
        assert client.placements == []


async def test_an_unrecognised_action_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session)
        result = await decide(session, client, approval.reference_id, "maybe")
        assert result.status == BLOCKED
        assert client.placements == []
        await clean(session)


async def test_the_submission_is_linked_back_to_the_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """So an audit can go from an order at the broker to the person who allowed it."""
    patch_green(monkeypatch)
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        approval = await pending_approval(session)
        result = await decide(session, FakeClient(), approval.reference_id, "approve")
        assert result.submission is not None
        assert result.submission.approval_reference == approval.reference_id
        await clean(session)


# --- housekeeping ---------------------------------------------------------


async def test_unanswered_approvals_are_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the record stops distinguishing "not yet" from "never"."""
    async with SessionLocal() as session:
        await clean(session)
        await pending_approval(session, expires_in=-10)
        await pending_approval(session, expires_in=600)
        expired = await expire_stale_approvals(session)
        await session.commit()
        assert expired == 1
        statuses = {row.status for row in (await session.scalars(select(LiveOrderApproval))).all()}
        assert statuses == {EXPIRED, PENDING}
        await clean(session)


def test_the_message_names_the_side_in_words() -> None:
    """B and S are the broker's language, not the operator's."""
    approval = SimpleNamespace(
        transaction_type="S",
        quantity=5,
        trading_symbol="IDEA-EQ",
        exchange="NSE",
        price=Decimal("400"),
        product="I",
        price_type="LMT",
        expires_at=datetime.now(UTC),
    )
    assert "SELL" in approval_message(approval)


def test_approved_is_not_the_same_as_submitted() -> None:
    """Distinct states, because a yes that the system then refused is not a send."""
    assert APPROVED != SUBMITTED
