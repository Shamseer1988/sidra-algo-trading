"""The safety panel must describe the system that is actually running.

Every live field here was a hardcoded ``False`` while the environment refused
to boot in live mode at all. Once that refusal was removed the constants
survived, and the dashboard went on reporting a locked paper system that could
in fact place real orders. These tests exist so that cannot happen twice: they
fail if any of the three fields stops tracking its source.
"""

from types import SimpleNamespace

import pytest

from app.api.routes import safety as safety_routes


class FakeRedis:
    async def aclose(self) -> None:
        return None


def settings(*, mode: str = "PAPER", enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(application_mode=mode, live_trading_enabled=enabled, redis_url="redis://unused")


@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_redis(_settings: object) -> FakeRedis:
        return FakeRedis()

    async def stop_state(_redis: object) -> dict:
        return {}

    async def paper_enabled(_redis: object) -> bool:
        return True

    monkeypatch.setattr(safety_routes, "_redis", fake_redis)
    monkeypatch.setattr(safety_routes, "emergency_stop_state", stop_state)
    monkeypatch.setattr(safety_routes, "paper_tracking_enabled", paper_enabled)


def _readiness(monkeypatch: pytest.MonkeyPatch, *, overall_ready: bool) -> None:
    async def inspect(_session: object, _settings: object) -> SimpleNamespace:
        return SimpleNamespace(overall_ready=overall_ready)

    monkeypatch.setattr(safety_routes, "inspect_live_readiness", inspect)


@pytest.mark.asyncio
async def test_a_paper_runtime_reports_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    _readiness(monkeypatch, overall_ready=False)
    status = await safety_routes.get_safety_status(settings(), object())
    assert status.application_mode == "PAPER"
    assert status.live_trading_enabled is False
    assert status.live_execution_available is False


@pytest.mark.asyncio
async def test_an_armed_runtime_is_not_reported_as_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this file was written for."""
    _readiness(monkeypatch, overall_ready=True)
    status = await safety_routes.get_safety_status(settings(mode="LIVE", enabled=True), object())
    assert status.application_mode == "LIVE"
    assert status.live_trading_enabled is True
    assert status.live_execution_available is True


@pytest.mark.asyncio
async def test_live_configured_but_ungated_is_not_reported_as_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured for live is not the same as able to send: the gates still hold."""
    _readiness(monkeypatch, overall_ready=False)
    status = await safety_routes.get_safety_status(settings(mode="LIVE", enabled=True), object())
    assert status.live_trading_enabled is True
    assert status.live_execution_available is False


@pytest.mark.asyncio
async def test_availability_follows_readiness_not_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lapsed activation turns overall_ready false, and the panel must follow."""
    armed = settings(mode="LIVE", enabled=True)
    _readiness(monkeypatch, overall_ready=True)
    assert (await safety_routes.get_safety_status(armed, object())).live_execution_available is True
    _readiness(monkeypatch, overall_ready=False)
    assert (await safety_routes.get_safety_status(armed, object())).live_execution_available is False
