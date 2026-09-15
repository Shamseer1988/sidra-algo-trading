"""Wiring live shadow evaluation into paper execution.

Paper execution is the system currently producing the operator's results. The
shadow evaluator observes it. So the tests here are almost entirely about what
the runner refuses to do: authenticate when the feature is off, spend a login
per signal, or let any failure of its own reach the caller.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import live_shadow_runner
from app.services.firstock.client import FirstockError, FirstockSession


@pytest.fixture(autouse=True)
def clear_cached_login():
    live_shadow_runner.reset_cached_login()
    yield
    live_shadow_runner.reset_cached_login()


def settings(*, enabled: bool = True, configured: bool = True, ttl_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        live_shadow_enabled=enabled,
        firstock_is_configured=configured,
        live_shadow_session_ttl_minutes=ttl_minutes,
    )


def signal() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


class LoginRecorder:
    def __init__(self, raises: Exception | None = None) -> None:
        self.calls = 0
        self._raises = raises

    def __call__(self, _settings: object) -> "LoginRecorder":
        return self

    async def login(self) -> FirstockSession:
        self.calls += 1
        if self._raises:
            raise self._raises
        return FirstockSession(user_id="user", session_token="jkey")


def patch_login(monkeypatch: pytest.MonkeyPatch, recorder: LoginRecorder) -> None:
    monkeypatch.setattr(live_shadow_runner, "FirstockClient", recorder)


def patch_evaluation(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    async def _shadow(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(live_shadow_runner, "shadow_paper_signal", _shadow)


async def test_a_disabled_feature_never_contacts_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LoginRecorder()
    patch_login(monkeypatch, recorder)
    await live_shadow_runner.run_live_shadow(settings(enabled=False), signal(), None)
    assert recorder.calls == 0


async def test_an_unconfigured_broker_never_attempts_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling the feature without credentials is a no-op, not a crash loop."""
    recorder = LoginRecorder()
    patch_login(monkeypatch, recorder)
    await live_shadow_runner.run_live_shadow(settings(configured=False), signal(), None)
    assert recorder.calls == 0


async def test_a_failed_login_records_nothing_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row per signal saying "could not log in" would bury the real refusals."""
    patch_login(monkeypatch, LoginRecorder(raises=FirstockError("bad TOTP")))
    calls: list = []
    patch_evaluation(monkeypatch, calls)
    await live_shadow_runner.run_live_shadow(settings(), signal(), None)
    assert calls == []


async def test_an_unexpected_login_error_is_also_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_login(monkeypatch, LoginRecorder(raises=RuntimeError("socket exploded")))
    await live_shadow_runner.run_live_shadow(settings(), signal(), None)


async def test_a_failure_inside_evaluation_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the guarantee paper execution depends on."""
    patch_login(monkeypatch, LoginRecorder())

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("evaluation is broken")

    monkeypatch.setattr(live_shadow_runner, "shadow_paper_signal", _explode)
    await live_shadow_runner.run_live_shadow(settings(), signal(), None)


async def test_the_login_is_reused_rather_than_repeated_per_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = LoginRecorder()
    patch_login(monkeypatch, recorder)
    patch_evaluation(monkeypatch, [])
    for _ in range(3):
        await live_shadow_runner.run_live_shadow(settings(), signal(), None)
    assert recorder.calls == 1


async def test_the_login_is_refreshed_once_the_ttl_lapses(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LoginRecorder()
    patch_login(monkeypatch, recorder)
    patch_evaluation(monkeypatch, [])
    await live_shadow_runner.run_live_shadow(settings(ttl_minutes=30), signal(), None)

    assert live_shadow_runner._cached_login is not None
    live_shadow_runner._cached_login.obtained_at = datetime.now(UTC) - timedelta(minutes=31)

    await live_shadow_runner.run_live_shadow(settings(ttl_minutes=30), signal(), None)
    assert recorder.calls == 2


async def test_a_failed_refresh_clears_the_cache_rather_than_keeping_a_dead_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_login(monkeypatch, LoginRecorder(raises=FirstockError("session expired")))
    await live_shadow_runner.run_live_shadow(settings(), signal(), None)
    assert live_shadow_runner._cached_login is None
