"""Whether a live order may be sent, and the order in which that is written down.

Two things are under test. First, that every gate refuses on its own — a gate
that can be satisfied by another gate passing is not a gate. Second, that the
submission record is durable before the request leaves, because that is the only
thing standing between a lost response and an untracked live position.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models import LiveActivation, LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.firstock.orders import FirstockTransportUnknown
from app.services.live_execution import authorize_live_submission, submit_live_order
from app.services.live_orders import LiveOrderRequest

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
    def __init__(self, state: dict | None = None, raises: Exception | None = None) -> None:
        self._state = state or {}
        self._raises = raises

    async def hgetall(self, _key: str) -> dict:
        if self._raises:
            raise self._raises
        return self._state


class FakeClient:
    """Read side answers margin; write side records placements."""

    def __init__(self, place=None, place_raises: Exception | None = None) -> None:
        self._place = place if place is not None else {"orderNumber": "24091500001"}
        self._place_raises = place_raises
        self.placements: list[dict] = []

    async def order_margin(self, **_kwargs: object) -> dict:
        return {"availableMargin": "500000", "marginOnNewOrder": "4180"}

    async def place_order(self, **kwargs: object) -> object:
        self.placements.append(dict(kwargs))
        if self._place_raises:
            raise self._place_raises
        return self._place


def readiness(ready: bool):
    async def _inspect(_session: object, _settings: object) -> SimpleNamespace:
        return SimpleNamespace(overall_ready=ready, gates=[SimpleNamespace(key="runtime_mode", passed=ready)])

    return _inspect


async def clean(session) -> None:  # noqa: ANN001
    await session.execute(delete(LiveOrderSubmission))
    await session.execute(delete(LiveActivation))
    await session.commit()


async def arm(session, *, minutes: int = 60) -> LiveActivation:  # noqa: ANN001
    record = LiveActivation(
        reason="test activation",
        gate_snapshot={},
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )
    session.add(record)
    await session.commit()
    return record


def patch_everything_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readiness and reconciliation both green, so one gate can be tested at a time."""
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(True))

    async def _recon_check(_session: object):
        from app.services.live_risk import LiveRiskCheck

        return LiveRiskCheck("reconciliation", True, "clean")

    monkeypatch.setattr("app.services.live_risk._reconciliation_check", _recon_check)


def gate(decision, key: str):
    return next(item for item in decision.gates if item.key == key)


async def authorize(monkeypatch, **overrides):
    patch_everything_green(monkeypatch)
    async with SessionLocal() as session:
        await clean(session)
        if overrides.pop("armed", True):
            await arm(session, minutes=overrides.pop("armed_minutes", 60))
        else:
            overrides.pop("armed_minutes", None)
        redis = overrides.pop("redis", None) or FakeRedis()
        client = overrides.pop("client", None) or FakeClient()
        decision = await authorize_live_submission(
            session,
            SimpleNamespace(),
            client,
            redis,
            approval_mode=overrides.pop("approval_mode", "AUTOMATIC"),
            request=overrides.pop("request", REQUEST),
            operator_approved=overrides.pop("operator_approved", None),
        )
        await clean(session)
        return decision


# --- each gate refuses on its own ----------------------------------------


async def test_every_gate_green_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch)
    assert decision.authorized is True
    assert decision.failures == []


async def test_an_engaged_emergency_stop_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis({"active": "true", "reason": "operator pressed stop"})
    decision = await authorize(monkeypatch, redis=redis)
    assert decision.authorized is False
    assert "operator pressed stop" in gate(decision, "emergency_stop").detail


async def test_an_unreadable_emergency_stop_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill switch that fails open is not a kill switch."""
    decision = await authorize(monkeypatch, redis=FakeRedis(raises=RuntimeError("redis down")))
    assert decision.authorized is False
    assert gate(decision, "emergency_stop").passed is False


async def test_without_an_activation_nothing_is_authorised(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, armed=False)
    assert decision.authorized is False
    assert "never been activated" in gate(decision, "activation").detail


async def test_an_expired_activation_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system armed this morning must not still be armed unattended tonight."""
    decision = await authorize(monkeypatch, armed_minutes=-1)
    assert decision.authorized is False
    assert "expired" in gate(decision, "activation").detail


@pytest.mark.parametrize("mode", ["DISABLED", "", "nonsense"])
async def test_only_the_two_documented_modes_permit_submission(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    decision = await authorize(monkeypatch, approval_mode=mode)
    assert decision.authorized is False
    assert gate(decision, "approval_mode").passed is False


async def test_telegram_mode_without_an_answer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absence of an answer is not an answer."""
    decision = await authorize(monkeypatch, approval_mode="TELEGRAM_APPROVAL", operator_approved=None)
    assert decision.authorized is False
    assert gate(decision, "operator_approval").passed is False


async def test_telegram_mode_with_a_rejection_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, approval_mode="TELEGRAM_APPROVAL", operator_approved=False)
    assert decision.authorized is False


async def test_telegram_mode_with_an_approval_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, approval_mode="TELEGRAM_APPROVAL", operator_approved=True)
    assert decision.authorized is True


async def test_automatic_mode_does_not_require_an_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, approval_mode="AUTOMATIC")
    assert decision.authorized is True
    assert [item.key for item in decision.gates].count("operator_approval") == 0


async def test_a_failing_live_risk_engine_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        decision = await authorize_live_submission(
            session,
            SimpleNamespace(),
            FakeClient(),
            FakeRedis(),
            approval_mode="AUTOMATIC",
            request=REQUEST,
        )
        await clean(session)
    assert decision.authorized is False
    assert gate(decision, "live_risk").passed is False


# --- an unresolved earlier submission blocks everything ------------------


async def test_an_unresolved_submission_blocks_the_whole_live_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending another while one may already be live is how a position doubles."""
    patch_everything_green(monkeypatch)
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        session.add(
            LiveOrderSubmission(
                client_order_id="sidra-stuck",
                exchange="NSE",
                trading_symbol="OTHER-EQ",
                product="I",
                price_type="LMT",
                transaction_type="B",
                quantity=1,
                price=Decimal("10"),
                status="UNKNOWN",
            )
        )
        await session.commit()
        decision = await authorize_live_submission(
            session,
            SimpleNamespace(),
            FakeClient(),
            FakeRedis(),
            approval_mode="AUTOMATIC",
            request=REQUEST,
        )
        await clean(session)
    assert decision.authorized is False
    assert "sidra-stuck" in gate(decision, "unresolved_submissions").detail


# --- write-ahead ----------------------------------------------------------


async def test_a_refused_order_writes_no_submission_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_everything_green(monkeypatch)
    client = FakeClient()
    async with SessionLocal() as session:
        await clean(session)
        decision, record = await submit_live_order(
            session, SimpleNamespace(), client, FakeRedis(), approval_mode="AUTOMATIC", request=REQUEST
        )
        rows = list((await session.scalars(select(LiveOrderSubmission))).all())
        await clean(session)
    assert decision.authorized is False
    assert record is None
    assert client.placements == []
    assert rows == []


async def test_an_accepted_order_is_recorded_with_its_broker_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_everything_green(monkeypatch)
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        _, record = await submit_live_order(
            session, SimpleNamespace(), FakeClient(), FakeRedis(), approval_mode="AUTOMATIC", request=REQUEST
        )
        assert record is not None
        assert record.status == "ACCEPTED"
        assert record.broker_order_numbers == ["24091500001"]
        assert record.sent_at is not None
        await clean(session)


async def test_a_lost_response_leaves_a_durable_unknown_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record exists precisely because the outcome does not."""
    patch_everything_green(monkeypatch)
    client = FakeClient(place_raises=FirstockTransportUnknown("placeOrder timed out"))
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        _, record = await submit_live_order(
            session, SimpleNamespace(), client, FakeRedis(), approval_mode="AUTOMATIC", request=REQUEST
        )
        assert record is not None
        assert record.status == "UNKNOWN"
        assert record.resolved_at is None
        stored = await session.scalar(
            select(LiveOrderSubmission).where(LiveOrderSubmission.client_order_id == record.client_order_id)
        )
        assert stored is not None
        await clean(session)


async def test_the_intent_survives_a_failure_inside_our_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local exception after the send may still have sent it."""
    patch_everything_green(monkeypatch)

    class Exploding(FakeClient):
        async def place_order(self, **kwargs: object) -> object:
            raise RuntimeError("something in our code broke")

    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        _, record = await submit_live_order(
            session, SimpleNamespace(), Exploding(), FakeRedis(), approval_mode="AUTOMATIC", request=REQUEST
        )
        assert record is not None
        assert record.status == "UNKNOWN"
        await clean(session)


async def test_a_second_order_is_refused_while_the_first_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end shape of the duplicate-order failure, prevented."""
    patch_everything_green(monkeypatch)
    async with SessionLocal() as session:
        await clean(session)
        await arm(session)
        await submit_live_order(
            session,
            SimpleNamespace(),
            FakeClient(place_raises=FirstockTransportUnknown("timeout")),
            FakeRedis(),
            approval_mode="AUTOMATIC",
            request=REQUEST,
        )
        second_client = FakeClient()
        decision, record = await submit_live_order(
            session, SimpleNamespace(), second_client, FakeRedis(), approval_mode="AUTOMATIC", request=REQUEST
        )
        await clean(session)
    assert decision.authorized is False
    assert record is None
    assert second_client.placements == []


async def test_the_snapshot_is_serialisable_for_the_audit_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    decision = await authorize(monkeypatch, armed=False)
    assert json.loads(json.dumps(decision.snapshot()))["authorized"] is False
