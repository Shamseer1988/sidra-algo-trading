"""Live readiness gates.

The report is what an operator reads to decide whether the system may go live,
so the property under test is that each gate describes the system as it actually
is. A gate that reads green for a condition nobody checked is the failure mode
worth writing tests against.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import live_readiness


class FakeSession:
    """Only the two calls readiness makes: a health probe and one scalar query."""

    def __init__(self, reconciliation: object | None = None) -> None:
        self._reconciliation = reconciliation
        self.added: list[object] = []

    async def execute(self, _query: object) -> object:
        return object()

    async def scalar(self, _query: object) -> object | None:
        return self._reconciliation

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class FakeRedis:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def settings() -> SimpleNamespace:
    """Every attestation a configuration file can assert, asserted."""
    return SimpleNamespace(
        application_mode="PAPER",
        live_trading_enabled=False,
        live_compliance_approved=True,
        live_static_ip_verified=True,
        redis_url="redis://unused",
    )


def live_reconciliation(*, safe: bool = True, age: timedelta = timedelta(minutes=1)) -> SimpleNamespace:
    return SimpleNamespace(
        safe_to_trade=safe,
        detail="blocked: untracked broker order" if not safe else "clean",
        created_at=datetime.now(UTC) - age,
    )


async def inspect(monkeypatch: pytest.MonkeyPatch, reconciliation: object | None = None):
    monkeypatch.setattr(live_readiness.Redis, "from_url", lambda *_args, **_kwargs: FakeRedis())
    report = await live_readiness.inspect_live_readiness(FakeSession(reconciliation), settings())  # type: ignore[arg-type]
    return report, {gate.key: gate for gate in report.gates}


async def test_the_system_stays_locked_with_every_attestation_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configuration can open the lock, because the lock is about code."""
    report, gates = await inspect(monkeypatch, live_reconciliation())
    assert report.status == "HARD_LOCKED"
    assert report.overall_ready is False
    assert gates["broker_adapter"].passed is False
    assert gates["administrator_activation"].passed is False
    assert report.snapshot()["broker_submission_permitted"] is False


async def test_gates_that_became_true_now_report_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """These two described a system without a live-execution layer. It has one now."""
    _, gates = await inspect(monkeypatch, live_reconciliation())
    assert gates["live_risk_engine"].passed is True
    assert gates["external_reconciliation"].passed is True


async def test_no_live_reconciliation_fails_the_reconciliation_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, gates = await inspect(monkeypatch, None)
    assert gates["external_reconciliation"].passed is False
    assert "No live reconciliation" in gates["external_reconciliation"].detail


async def test_a_blocked_live_reconciliation_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _, gates = await inspect(monkeypatch, live_reconciliation(safe=False))
    assert gates["external_reconciliation"].passed is False
    assert "untracked broker order" in gates["external_reconciliation"].detail


async def test_a_stale_live_reconciliation_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = live_reconciliation(age=live_readiness.RECONCILIATION_FRESHNESS + timedelta(minutes=1))
    _, gates = await inspect(monkeypatch, stale)
    assert gates["external_reconciliation"].passed is False
    assert "minutes old" in gates["external_reconciliation"].detail


async def test_a_naive_reconciliation_timestamp_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres can return a naive datetime; comparing it must not explode."""
    naive = SimpleNamespace(safe_to_trade=True, detail="clean", created_at=datetime.now(UTC).replace(tzinfo=None))
    _, gates = await inspect(monkeypatch, naive)
    assert gates["external_reconciliation"].passed is True


async def test_overall_readiness_is_derived_from_the_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derived, so a gate nobody wired in cannot leave the report claiming readiness."""
    report, gates = await inspect(monkeypatch, live_reconciliation())
    assert report.overall_ready == all(gate.passed for gate in gates.values())


def test_the_submission_lock_is_a_statement_about_the_code() -> None:
    """If this ever flips without a submission module existing, the gate is a lie."""
    assert live_readiness.SUBMISSION_ADAPTER_IMPLEMENTED is False
    with pytest.raises(ImportError):
        __import__("app.services.live_orders")
