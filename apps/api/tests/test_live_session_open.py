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


# --- keeping the reconciliation fresh ---------------------------------------
#
# A reconciliation is valid for fifteen minutes; the scheduled open reconciles
# once and arms for eight hours. Without a refresh the verdict expires at 09:00
# and every signal for the rest of the session is refused -- armed, healthy, and
# unable to trade. That is what happened on the first live day.


def market(phase: str = "OPEN", trading: bool = True) -> SimpleNamespace:
    from app.services.trading_calendar import MarketPhase

    return SimpleNamespace(
        status_at=lambda _ts: SimpleNamespace(
            trading_day=trading,
            phase=getattr(MarketPhase, phase),
            reason="Regular session" if trading else "Exchange holiday",
        )
    )


def test_the_two_freshness_windows_agree_and_are_shorter_than_an_activation() -> None:
    """The bug was a mismatch nobody had written down. Now it fails a test."""
    from app.core.config import Settings
    from app.services.live_readiness import RECONCILIATION_FRESHNESS
    from app.services.live_risk import RECONCILIATION_MAX_AGE

    assert RECONCILIATION_FRESHNESS == RECONCILIATION_MAX_AGE, (
        "The readiness gate and the risk engine must expire a reconciliation together; "
        "if they drift, one will authorise what the other refuses."
    )
    ttl_minutes = Settings.model_fields["live_activation_ttl_minutes"].default
    assert RECONCILIATION_FRESHNESS.total_seconds() / 60 < ttl_minutes, (
        "An activation outlives a reconciliation, so something must refresh it during the session."
    )


@pytest.fixture
def refresh_wiring(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(session=FakeSession(), armed=True, previous_safe=True, record=None, reconciled=False)
    state.record = reconciliation(safe=True)

    monkeypatch.setattr(module, "SessionLocal", lambda: state.session)
    monkeypatch.setattr(module, "live_report_adapter", _async(object()))

    async def current(_session):
        return SimpleNamespace(expires_at=datetime.now(UTC) + timedelta(hours=2)) if state.armed else None

    async def reconcile(_session, _adapter):
        state.reconciled = True
        return object()

    async def persist(_session, _report):
        return state.record

    async def scalar(*_args, **_kwargs):
        return SimpleNamespace(safe_to_trade=state.previous_safe)

    state.session.scalar = scalar
    monkeypatch.setattr(module, "current_activation", current)
    monkeypatch.setattr(module, "reconcile_live_execution", reconcile)
    monkeypatch.setattr(module, "persist_live_reconciliation", persist)
    return state


@pytest.mark.asyncio
async def test_an_open_armed_session_is_refreshed(refresh_wiring) -> None:
    result = await module.refresh_reconciliation(settings(), market())
    assert result.ran is True
    assert result.safe_to_trade is True
    assert refresh_wiring.reconciled is True


@pytest.mark.asyncio
async def test_a_disarmed_session_spends_no_broker_calls(refresh_wiring) -> None:
    refresh_wiring.armed = False
    result = await module.refresh_reconciliation(settings(), market())
    assert result.ran is False
    assert result.step == "disarmed"
    assert refresh_wiring.reconciled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["CLOSED", "POST_MARKET"])
async def test_a_closed_exchange_is_not_refreshed(refresh_wiring, phase: str) -> None:
    result = await module.refresh_reconciliation(settings(), market(phase=phase))
    assert result.ran is False
    assert result.step == "closed"
    assert refresh_wiring.reconciled is False


@pytest.mark.asyncio
async def test_a_holiday_is_not_refreshed(refresh_wiring) -> None:
    result = await module.refresh_reconciliation(settings(), market(trading=False))
    assert result.ran is False
    assert refresh_wiring.reconciled is False


@pytest.mark.asyncio
async def test_a_paper_runtime_is_not_refreshed(refresh_wiring) -> None:
    result = await module.refresh_reconciliation(settings(mode="PAPER", enabled=False), market())
    assert result.ran is False
    assert result.step == "runtime"
    assert refresh_wiring.reconciled is False


@pytest.mark.asyncio
async def test_going_from_clean_to_blocked_is_reported_as_a_change(refresh_wiring) -> None:
    """The message that matters: trading has just stopped."""
    refresh_wiring.previous_safe = True
    refresh_wiring.record = reconciliation(safe=False)
    result = await module.refresh_reconciliation(settings(), market())
    assert result.ran is True
    assert result.safe_to_trade is False
    assert result.changed is True
    assert result.findings == ["Broker order 123 (REJECTED) has no local record."]


@pytest.mark.asyncio
async def test_still_clean_is_not_news(refresh_wiring) -> None:
    """Ten-minute 'still fine' messages are how an alert channel stops being read."""
    refresh_wiring.previous_safe = True
    refresh_wiring.record = reconciliation(safe=True)
    result = await module.refresh_reconciliation(settings(), market())
    assert result.changed is False


@pytest.mark.asyncio
async def test_recovery_is_reported(refresh_wiring) -> None:
    refresh_wiring.previous_safe = False
    refresh_wiring.record = reconciliation(safe=True)
    result = await module.refresh_reconciliation(settings(), market())
    assert result.changed is True
    assert result.safe_to_trade is True


@pytest.mark.asyncio
async def test_an_unreachable_broker_does_not_kill_the_job(refresh_wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "live_report_adapter", _async(RuntimeError("timeout")))
    result = await module.refresh_reconciliation(settings(), market())
    assert result.ran is False
    assert result.step == "broker"
