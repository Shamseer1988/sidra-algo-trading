from types import SimpleNamespace

import pytest

from app.services import live_readiness


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, _query: object) -> object:
        return object()

    async def scalar(self, _query: object) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class FakeRedis:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_live_readiness_is_hard_locked_even_when_external_attestations_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_readiness.Redis, "from_url", lambda *_args, **_kwargs: FakeRedis())
    settings = SimpleNamespace(
        application_mode="PAPER",
        live_trading_enabled=False,
        live_compliance_approved=True,
        live_static_ip_verified=True,
        redis_url="redis://unused",
    )
    report = await live_readiness.inspect_live_readiness(FakeSession(), settings)  # type: ignore[arg-type]

    gates = {gate.key: gate for gate in report.gates}
    assert report.status == "HARD_LOCKED"
    assert report.overall_ready is False
    assert gates["runtime_lock"].passed is True
    assert gates["broker_adapter"].passed is False
    assert gates["live_risk_engine"].passed is False
    assert gates["administrator_activation"].passed is False
    assert report.snapshot()["broker_submission_permitted"] is False
