"""Live shadow evaluation: what the live path would have done, recorded.

These tests run against the real database rather than a fake session, because
two of the properties that matter are database properties: one decision per
paper signal, and a snapshot that survives a round trip through JSON columns.

The behaviour under the most scrutiny is the isolation. Paper execution is the
system currently producing the operator's results; a shadow evaluator that can
raise into it has made the system worse, not safer.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models import LiveShadowDecision, PaperSignal
from app.db.session import SessionLocal
from app.services.firstock.orders import FirstockTransportUnknown
from app.services.live_shadow import (
    build_shadow_payload,
    evaluate_live_shadow,
    product_for,
    shadow_paper_signal,
    summarize_live_shadow,
    transaction_type_for,
)


class FakeClient:
    """Records whether the broker was consulted at all."""

    def __init__(self, margin: dict | None = None, raises: Exception | None = None) -> None:
        self._margin = margin or {"availableMargin": "50000", "marginOnNewOrder": "4180"}
        self._raises = raises
        self.calls = 0

    async def order_margin(self, **_kwargs: object) -> dict:
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._margin


def readiness(ready: bool):
    async def _inspect(_session: object, _settings: object) -> SimpleNamespace:
        return SimpleNamespace(overall_ready=ready, gates=[SimpleNamespace(key="broker_adapter", passed=ready)])

    return _inspect


def reconciliation(safe: bool = True) -> SimpleNamespace:
    return SimpleNamespace(safe_to_trade=safe, detail="clean", created_at=datetime.now(UTC))


async def make_signal(session, *, token: str = "NSE_EQ|INE002A01018", side: str = "LONG") -> PaperSignal:
    signal = PaperSignal(
        signal_key=f"shadow-{uuid4()}",
        instrument_token=token,
        session_date=datetime(2026, 9, 15, tzinfo=UTC).date(),
        candle_opened_at=datetime(2026, 9, 15, tzinfo=UTC),
        strategy_version="orb-retest-v1@1",
        side=side,
        entry_price=Decimal("418"),
        stop_price=Decimal("410"),
        target_price=Decimal("430"),
        quantity=10,
        risk_amount=Decimal("80"),
        score=90,
        score_breakdown={},
        strategy_snapshot={},
        indicator_snapshot={},
    )
    session.add(signal)
    await session.commit()
    await session.refresh(signal)
    return signal


async def cleanup(session, signal: PaperSignal) -> None:
    await session.execute(delete(LiveShadowDecision).where(LiveShadowDecision.paper_signal_id == signal.id))
    await session.execute(delete(PaperSignal).where(PaperSignal.id == signal.id))
    await session.commit()


# --- payload construction -------------------------------------------------


def test_product_follows_the_leverage_control_rather_than_a_constant() -> None:
    assert product_for(True) == "I"
    assert product_for(False) == "C"


def test_sides_map_to_the_documented_transaction_types() -> None:
    assert transaction_type_for("LONG") == "B"
    assert transaction_type_for("SHORT") == "S"
    assert transaction_type_for("short") == "S"


def test_an_unknown_side_raises_rather_than_defaulting_to_buy() -> None:
    with pytest.raises(ValueError, match="Unsupported signal side"):
        transaction_type_for("SIDEWAYS")


async def test_payload_addresses_the_order_as_the_broker_expects() -> None:
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            payload, translation = await build_shadow_payload(session, signal, intraday_leverage_enabled=True)
            assert translation.resolved is True
            assert payload is not None
            assert payload.trading_symbol == "RELIANCE-EQ"
            assert payload.exchange == "NSE"
            assert payload.product == "I"
            assert payload.price_type == "LMT"
            assert payload.transaction_type == "B"
            assert payload.price == "418.0000"
            assert payload.quantity == "10"
        finally:
            await cleanup(session, signal)


async def test_an_unresolvable_symbol_yields_no_payload() -> None:
    async with SessionLocal() as session:
        signal = await make_signal(session, token="NSE_EQ|INE999Z01099")
        try:
            payload, translation = await build_shadow_payload(session, signal, intraday_leverage_enabled=True)
            assert payload is None
            assert translation.resolved is False
        finally:
            await cleanup(session, signal)


async def test_an_unsupported_side_is_a_refusal_not_an_exception() -> None:
    """build_shadow_payload is called from the paper path; it must not raise."""
    async with SessionLocal() as session:
        signal = await make_signal(session, side="FLAT")
        try:
            payload, translation = await build_shadow_payload(session, signal, intraday_leverage_enabled=True)
            assert payload is None
            assert "Unsupported signal side" in translation.reason
        finally:
            await cleanup(session, signal)


# --- evaluation -----------------------------------------------------------


async def test_an_unresolvable_symbol_never_reaches_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is nothing to ask about an instrument we cannot name."""
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(True))
    client = FakeClient()
    async with SessionLocal() as session:
        signal = await make_signal(session, token="NSE_EQ|INE999Z01099")
        try:
            record = await evaluate_live_shadow(
                session,
                SimpleNamespace(),
                client,
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            assert client.calls == 0
            assert record.authorized is False
            assert record.translation_status == "UNRESOLVED"
            assert record.trading_symbol is None
            assert record.failed_checks == ["symbol_translation"]
        finally:
            await cleanup(session, signal)


async def test_a_resolvable_symbol_records_the_full_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            record = await evaluate_live_shadow(
                session,
                SimpleNamespace(),
                FakeClient(),
                signal=signal,
                oms_order_id=None,
                approval_mode="TELEGRAM_APPROVAL",
                intraday_leverage_enabled=True,
            )
            assert record.translation_status == "RESOLVED"
            assert record.trading_symbol == "RELIANCE-EQ"
            assert record.approval_mode == "TELEGRAM_APPROVAL"
            # The readiness gates are closed, so this must refuse.
            assert record.authorized is False
            assert "live_readiness" in record.failed_checks
            assert record.decision_snapshot["checks"]
        finally:
            await cleanup(session, signal)


async def test_broker_margin_numbers_are_stored_as_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored structurally so a later review can compare them against position size."""
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            record = await evaluate_live_shadow(
                session,
                SimpleNamespace(),
                FakeClient({"availableMargin": "50000", "marginOnNewOrder": "4180"}),
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            assert record.broker_margin_required == Decimal("4180.00")
            assert record.broker_margin_available == Decimal("50000.00")
        finally:
            await cleanup(session, signal)


async def test_a_broker_outage_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            record = await evaluate_live_shadow(
                session,
                SimpleNamespace(),
                FakeClient(raises=FirstockTransportUnknown("orderMargin timed out")),
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            assert record.authorized is False
            assert "broker_margin" in record.failed_checks
        finally:
            await cleanup(session, signal)


# --- isolation from the paper path ---------------------------------------


async def test_shadow_paper_signal_swallows_a_failure_in_this_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug here must cost an evidence row, never a paper trade."""

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("shadow evaluation is broken")

    monkeypatch.setattr("app.services.live_shadow.evaluate_live_shadow", _explode)
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            result = await shadow_paper_signal(
                session,
                SimpleNamespace(),
                FakeClient(),
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            assert result is None
        finally:
            await cleanup(session, signal)


async def test_the_same_signal_is_never_evaluated_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried scanner pass must not inflate the authorisation statistics."""
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        signal = await make_signal(session)
        try:
            first = await shadow_paper_signal(
                session,
                SimpleNamespace(),
                FakeClient(),
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            await session.commit()
            second = await shadow_paper_signal(
                session,
                SimpleNamespace(),
                FakeClient(),
                signal=signal,
                oms_order_id=None,
                approval_mode="AUTOMATIC",
                intraday_leverage_enabled=True,
            )
            assert first is not None
            assert second is None
            count = await session.scalar(
                select(LiveShadowDecision.id).where(LiveShadowDecision.paper_signal_id == signal.id)
            )
            assert count is not None
        finally:
            await cleanup(session, signal)


# --- summary --------------------------------------------------------------


async def test_summary_reports_the_authorisation_rate_and_why_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(False))
    async with SessionLocal() as session:
        await session.execute(delete(LiveShadowDecision))
        await session.commit()
        signals = [await make_signal(session) for _ in range(2)]
        signals.append(await make_signal(session, token="NSE_EQ|INE999Z01099"))
        try:
            for signal in signals:
                await shadow_paper_signal(
                    session,
                    SimpleNamespace(),
                    FakeClient(),
                    signal=signal,
                    oms_order_id=None,
                    approval_mode="AUTOMATIC",
                    intraday_leverage_enabled=True,
                )
            await session.commit()
            summary = await summarize_live_shadow(session)
            assert summary.evaluated == 3
            assert summary.authorized == 0
            assert summary.refused == 3
            assert summary.unresolved_symbols == 1
            assert summary.authorization_rate_percent == 0.0
            assert {item["check"] for item in summary.top_refusals} >= {"live_readiness", "symbol_translation"}
        finally:
            for signal in signals:
                await cleanup(session, signal)


async def test_summary_of_an_empty_table_does_not_divide_by_zero() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(LiveShadowDecision))
        await session.commit()
        summary = await summarize_live_shadow(session)
        assert summary.evaluated == 0
        assert summary.authorization_rate_percent == 0.0
