"""The scheduled open must refuse far more reliably than it succeeds.

This job arms a live trading account with nobody watching, so each test here
pins a way it must decline. The success case is one test; the rest are the
reasons it must not reach one.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import live_session_open as module


class FakeRedis:
    def __init__(self, stopped: str = "false") -> None:
        self.stopped = stopped
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def aclose(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = 0
        self.rolled_back = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def refresh(self, _value: object) -> None:
        return None

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def settings(*, mode: str = "LIVE", enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        application_mode=mode,
        live_trading_enabled=enabled,
        redis_url="redis://unused",
        live_activation_ttl_minutes=480,
    )


def calendar(*, trading: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status_at=lambda _ts: SimpleNamespace(
            trading_day=trading, reason="Second Saturday" if not trading else "Regular session"
        )
    )


def reconciliation(*, safe: bool) -> SimpleNamespace:
    return SimpleNamespace(
        safe_to_trade=safe,
        detail="clean" if safe else "Trading blocked: 1 blocking (UNTRACKED_BROKER_ORDER), 0 for review.",
        findings=[] if safe else [{"detail": "Broker order 123 (REJECTED) has no local record."}],
    )


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    """Patch every collaborator; each test overrides only what it is about."""
    state = SimpleNamespace(session=FakeSession(), redis=FakeRedis(), armed=None, activation=None)

    monkeypatch.setattr(module, "SessionLocal", lambda: state.session)
    monkeypatch.setattr(module, "automation_user", _async(SimpleNamespace(id="user-automation")))
    monkeypatch.setattr(module, "current_activation", _async(None))
    monkeypatch.setattr(module, "live_report_adapter", _async(object()))
    monkeypatch.setattr(module, "reconcile_live_execution", _async(object()))
    monkeypatch.setattr(module, "persist_live_reconciliation", _async(reconciliation(safe=True)))
    monkeypatch.setattr(
        module, "inspect_live_readiness", _async(SimpleNamespace(blocking_activation=[], snapshot=dict))
    )

    async def activate(_session, _settings, _report, _user, *, reason):
        state.armed = reason
        state.activation = SimpleNamespace(reason=reason, expires_at=datetime.now(UTC) + timedelta(hours=8))
        return state.activation

    monkeypatch.setattr(module, "activate_live_trading", activate)

    async def start_scanner(_settings, session, user):
        state.redis.values[module.SCANNER_CONTROL_KEY] = "RUNNING"

    monkeypatch.setattr(module, "_start_scanner", start_scanner)
    return state


def _async(value):
    async def _call(*_args, **_kwargs):
        if isinstance(value, Exception):
            raise value
        return value

    return _call


@pytest.mark.asyncio
async def test_a_holiday_opens_nothing(wiring) -> None:
    result = await module.open_live_session(settings(), calendar(trading=False))
    assert result.opened is False
    assert result.step == "calendar"
    assert wiring.armed is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "enabled"),
    [("PAPER", False), ("PAPER", True), ("LIVE", False), ("REPLAY", True)],
)
async def test_a_runtime_not_configured_for_live_arms_nothing(wiring, mode: str, enabled: bool) -> None:
    """Both flags are required. Either one alone must open nothing."""
    result = await module.open_live_session(settings(mode=mode, enabled=enabled), calendar())
    assert result.opened is False
    assert result.step == "runtime"
    assert wiring.armed is None


@pytest.mark.asyncio
async def test_a_blocked_reconciliation_never_arms(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    """The regression that matters: arming over a broker state nobody checked."""
    monkeypatch.setattr(module, "persist_live_reconciliation", _async(reconciliation(safe=False)))
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is False
    assert result.step == "reconcile"
    assert wiring.armed is None
    assert "UNTRACKED_BROKER_ORDER" in result.detail
    assert result.findings == ["Broker order 123 (REJECTED) has no local record."]


@pytest.mark.asyncio
async def test_an_unreachable_broker_never_arms(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    monkeypatch.setattr(module, "live_report_adapter", _async(RuntimeError("connection refused")))
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is False
    assert result.step == "broker"
    assert wiring.armed is None


@pytest.mark.asyncio
async def test_a_refused_activation_is_reported_not_retried(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    async def refuse(*_args, **_kwargs):
        raise module.LiveActivationError("Live readiness gates are not satisfied: static_ip")

    monkeypatch.setattr(module, "activate_live_trading", refuse)
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is False
    assert result.step == "arm"
    assert "static_ip" in result.detail


@pytest.mark.asyncio
async def test_an_existing_activation_is_left_alone(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    """Re-running must not stack a second window over a live one."""
    expiry = datetime.now(UTC) + timedelta(hours=3)
    monkeypatch.setattr(module, "current_activation", _async(SimpleNamespace(expires_at=expiry)))
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is True
    assert result.step == "already_armed"
    assert wiring.armed is None


@pytest.mark.asyncio
async def test_a_clean_morning_arms_and_starts_the_scanner(wiring) -> None:
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is True
    assert result.step == "opened"
    assert wiring.armed.startswith("Scheduled live session ")
    assert wiring.redis.values[module.SCANNER_CONTROL_KEY] == "RUNNING"
    assert result.expires_at is not None


@pytest.mark.asyncio
async def test_a_scanner_failure_is_reported_rather_than_swallowed(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    """Armed but not scanning is a real state, and the operator must be told."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("Emergency stop is active")

    monkeypatch.setattr(module, "_start_scanner", boom)
    result = await module.open_live_session(settings(), calendar())
    assert result.opened is False
    assert result.step == "scanner"
    assert "Emergency stop" in result.detail
    assert result.expires_at is not None


@pytest.mark.asyncio
async def test_the_scanner_key_is_the_one_the_scanner_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local copy of this constant drifted once and wrote to a dead key.

    The scanner would have stayed stopped while everything else reported a
    successful open, so this asserts the identity rather than the value.
    """
    from app.api.routes import scanner as scanner_route
    from app.services import safety as safety_service

    assert module.SCANNER_CONTROL_KEY == safety_service.SCANNER_CONTROL_KEY
    assert module.SCANNER_CONTROL_KEY == scanner_route.SCANNER_CONTROL_KEY

    redis = FakeRedis()
    monkeypatch.setattr(module, "SessionLocal", lambda: FakeSession())

    class Factory:
        @staticmethod
        def from_url(*_args, **_kwargs):
            return redis

    monkeypatch.setitem(__import__("sys").modules, "redis.asyncio", SimpleNamespace(Redis=Factory))
    monkeypatch.setattr("app.services.safety.emergency_stop_state", _async({"active": "false"}))

    session = FakeSession()
    await module._start_scanner(settings(), session, SimpleNamespace(id="u"))
    assert redis.values == {"scanner:control_state": "RUNNING"}


@pytest.mark.asyncio
async def test_the_scanner_is_not_started_under_an_emergency_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()

    class Factory:
        @staticmethod
        def from_url(*_args, **_kwargs):
            return redis

    monkeypatch.setitem(__import__("sys").modules, "redis.asyncio", SimpleNamespace(Redis=Factory))
    monkeypatch.setattr("app.services.safety.emergency_stop_state", _async({"active": "true"}))

    with pytest.raises(RuntimeError, match="Emergency stop"):
        await module._start_scanner(settings(), FakeSession(), SimpleNamespace(id="u"))
    assert redis.values == {}


@pytest.mark.asyncio
async def test_a_dry_run_reconciles_but_never_arms(wiring) -> None:
    """The rehearsal must reach the reconcile and stop dead before the arm."""
    result = await module.open_live_session(settings(), calendar(), dry_run=True)
    assert result.opened is False
    assert result.step == "dry_run"
    assert wiring.armed is None
    assert module.SCANNER_CONTROL_KEY not in wiring.redis.values


@pytest.mark.asyncio
async def test_a_dry_run_still_reports_a_blocked_reconciliation(monkeypatch: pytest.MonkeyPatch, wiring) -> None:
    monkeypatch.setattr(module, "persist_live_reconciliation", _async(reconciliation(safe=False)))
    result = await module.open_live_session(settings(), calendar(), dry_run=True)
    assert result.step == "reconcile"
    assert wiring.armed is None
