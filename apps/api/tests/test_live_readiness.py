"""Live readiness gates, now that a submission adapter exists.

Before Phase 4 this report was advisory: nothing could place an order whatever
it said. It is now the thing that decides whether an administrator may arm live
trading, so each gate has to describe a condition somebody satisfied rather than
a state of the codebase.

The distinction these tests protect is between ``ready_for_activation`` — every
precondition met, so a person may arm it — and ``overall_ready``, which
additionally requires that they have. Conflating them is a deadlock: an
activation would be needed before an activation could be granted.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS
from app.db.models import ExecutionReconciliation, LiveActivation
from app.services import live_readiness


class FakeSession:
    """Answers the two scalar queries readiness makes, by entity rather than order.

    Plus the primary-key get that resolves the selected broker out of the
    trading controls.
    """

    def __init__(
        self,
        reconciliation: object | None = None,
        activation: object | None = None,
        broker: str = "UPSTOX",
    ) -> None:
        self._by_entity = {
            ExecutionReconciliation: reconciliation,
            LiveActivation: activation,
        }
        self._broker = broker
        self.added: list[object] = []

    async def execute(self, _query: object) -> object:
        return object()

    async def scalar(self, query: object) -> object | None:
        entity = query.column_descriptions[0]["entity"]
        return self._by_entity.get(entity)

    async def get(self, _entity: object, _key: object) -> object | None:
        if self._broker is None:
            return None
        return SimpleNamespace(value={**DEFAULT_TRADING_CONTROLS, "live_broker": self._broker})

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class FakeRedis:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def settings(*, live: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        application_mode="LIVE" if live else "PAPER",
        live_trading_enabled=live,
        live_compliance_approved=True,
        live_static_ip_verified=True,
        redis_url="redis://unused",
    )


def reconciliation(*, safe: bool = True, age: timedelta = timedelta(minutes=1)) -> SimpleNamespace:
    return SimpleNamespace(
        safe_to_trade=safe,
        detail="blocked: untracked broker order" if not safe else "clean",
        created_at=datetime.now(UTC) - age,
    )


def activation(*, expired: bool = False, revoked: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        expires_at=datetime.now(UTC) + (timedelta(minutes=-1) if expired else timedelta(hours=2)),
        revoked_at=datetime.now(UTC) if revoked else None,
    )


async def inspect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recon: object | None = None,
    armed: object | None = None,
    live: bool = True,
    broker: str = "UPSTOX",
):
    monkeypatch.setattr(live_readiness.Redis, "from_url", lambda *_args, **_kwargs: FakeRedis())
    report = await live_readiness.inspect_live_readiness(
        FakeSession(recon, armed, broker),  # type: ignore[arg-type]
        settings(live=live),
    )
    return report, {gate.key: gate for gate in report.gates}


# --- the two readiness questions -----------------------------------------


async def test_everything_met_but_unarmed_is_ready_to_arm_and_not_ready_to_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the whole activation flow rests on."""
    report, gates = await inspect(monkeypatch, recon=reconciliation(), armed=None)
    assert report.ready_for_activation is True
    assert report.overall_ready is False
    assert gates["administrator_activation"].passed is False


async def test_armed_and_everything_met_is_ready_to_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = await inspect(monkeypatch, recon=reconciliation(), armed=activation())
    assert report.overall_ready is True
    assert report.status == "READY"


async def test_overall_readiness_is_derived_from_the_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derived, so a gate nobody wired in cannot leave the report claiming readiness."""
    report, gates = await inspect(monkeypatch, recon=reconciliation(), armed=activation())
    assert report.overall_ready == all(gate.passed for gate in gates.values())


# --- broker selection -----------------------------------------------------


@pytest.mark.parametrize("broker", ["NONE", None])
async def test_no_selected_broker_blocks_activation(monkeypatch: pytest.MonkeyPatch, broker: str | None) -> None:
    """An operator reading this report should be told what is still missing.

    ``None`` stands for trading controls that have never been saved, which
    defaults to NONE — a refusal, not a hint to pick whichever broker happens to
    have credentials.
    """
    report, gates = await inspect(monkeypatch, recon=reconciliation(), armed=activation(), broker=broker)
    assert report.overall_ready is False
    assert gates["broker_selected"].passed is False
    assert "admin settings" in gates["broker_selected"].detail


@pytest.mark.parametrize("broker", ["UPSTOX", "FIRSTOCK"])
async def test_a_selected_broker_passes_and_is_named(monkeypatch: pytest.MonkeyPatch, broker: str) -> None:
    _, gates = await inspect(monkeypatch, recon=reconciliation(), armed=activation(), broker=broker)
    assert gates["broker_selected"].passed is True
    assert broker in gates["broker_selected"].detail


# --- runtime configuration ------------------------------------------------


async def test_a_paper_runtime_is_not_ready_for_live(monkeypatch: pytest.MonkeyPatch) -> None:
    report, gates = await inspect(monkeypatch, recon=reconciliation(), armed=activation(), live=False)
    assert report.overall_ready is False
    assert report.ready_for_activation is False
    assert gates["runtime_mode"].passed is False
    assert "APPLICATION_MODE is PAPER" in gates["runtime_mode"].detail
    assert "LIVE_TRADING_ENABLED is false" in gates["runtime_mode"].detail


# --- reconciliation -------------------------------------------------------


async def test_no_live_reconciliation_blocks_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    report, gates = await inspect(monkeypatch, recon=None, armed=activation())
    assert gates["external_reconciliation"].passed is False
    assert report.ready_for_activation is False


async def test_a_blocked_live_reconciliation_blocks_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    _, gates = await inspect(monkeypatch, recon=reconciliation(safe=False), armed=activation())
    assert gates["external_reconciliation"].passed is False
    assert "untracked broker order" in gates["external_reconciliation"].detail


async def test_a_stale_live_reconciliation_blocks_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = reconciliation(age=live_readiness.RECONCILIATION_FRESHNESS + timedelta(minutes=1))
    _, gates = await inspect(monkeypatch, recon=stale, armed=activation())
    assert gates["external_reconciliation"].passed is False
    assert "minutes old" in gates["external_reconciliation"].detail


async def test_a_naive_reconciliation_timestamp_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    naive = SimpleNamespace(safe_to_trade=True, detail="clean", created_at=datetime.now(UTC).replace(tzinfo=None))
    _, gates = await inspect(monkeypatch, recon=naive, armed=activation())
    assert gates["external_reconciliation"].passed is True


# --- activation -----------------------------------------------------------


@pytest.mark.parametrize("state", [{"expired": True}, {"revoked": True}])
async def test_an_expired_or_revoked_activation_does_not_count(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    report, gates = await inspect(monkeypatch, recon=reconciliation(), armed=activation(**state))
    assert gates["administrator_activation"].passed is False
    assert report.overall_ready is False


# --- the adapter claim ----------------------------------------------------


def test_the_gate_and_the_code_agree_that_an_adapter_exists() -> None:
    """A True here with no adapter would be a gate lying to the operator."""
    import app.services.live_orders  # noqa: F401 - existence is the assertion

    assert live_readiness.SUBMISSION_ADAPTER_IMPLEMENTED is True


async def test_the_snapshot_states_both_readiness_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = await inspect(monkeypatch, recon=reconciliation(), armed=None)
    snapshot = report.snapshot()
    assert snapshot["ready_for_activation"] is True
    assert snapshot["overall_ready"] is False
    assert snapshot["broker_submission_permitted"] is True
