"""Choosing the database address, which is decided once and lived with.

``DATABASE_URL`` names ``postgres``, the compose service. That name resolves
inside the compose network and nowhere else, so a script run straight on the
NAS falls back to the port compose publishes on 127.0.0.1. The fallback is for
the host. Inside a container it is never right -- 127.0.0.1 is the container
itself, and nothing listens there.

On 29 October the NAS restarted. Docker brought the containers back under
their restart policy, which does not honour ``depends_on``, so the worker
started before DNS could answer for ``postgres``. One failed lookup rewrote
the URL, the engine was built from it at import, and nothing reconsidered: the
same connection error every thirty seconds for four days, no candles stored,
and no orders on three trading days.
"""

import socket

import pytest

from app.db import session as module


class Resolver:
    """Stands in for gethostbyname, failing a set number of times first."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    def __call__(self, host: str) -> str:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise OSError(-2, "Name or service not known")
        return "172.18.0.2"


IN_NETWORK = "postgresql+asyncpg://user:pass@postgres:5432/intraday_sentinel"


def resolve(url: str = IN_NETWORK) -> str:
    return module._resolved_db_url(url, sleep=lambda _: None)


# --- the service name resolves ------------------------------------------


def test_a_name_that_resolves_is_left_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "gethostbyname", Resolver(0))
    assert resolve() == IN_NETWORK


def test_a_url_that_does_not_name_the_service_is_never_touched(monkeypatch: pytest.MonkeyPatch):
    """A test database, or any URL an operator set by hand, is theirs."""

    def explode(host: str) -> str:
        raise AssertionError("should not have looked anything up")

    monkeypatch.setattr(socket, "gethostbyname", explode)
    other = "postgresql+asyncpg://user:pass@10.0.0.9:5432/intraday_sentinel_test"
    assert resolve(other) == other


# --- the lookup is retried ----------------------------------------------


def test_a_slow_dns_is_waited_for_rather_than_given_up_on(monkeypatch: pytest.MonkeyPatch):
    """The whole fault: Docker's DNS trailed the container it had started.

    One lookup decided the address for the life of the process. Nine failures
    then an answer must end with the name, not the fallback.
    """
    resolver = Resolver(module.RESOLVE_ATTEMPTS - 1)
    monkeypatch.setattr(socket, "gethostbyname", resolver)
    assert resolve() == IN_NETWORK
    assert resolver.calls == module.RESOLVE_ATTEMPTS


def test_it_does_not_retry_forever(monkeypatch: pytest.MonkeyPatch):
    """Waiting is the supervisor's job past this point, not a sleep's."""
    resolver = Resolver(module.RESOLVE_ATTEMPTS + 5)
    monkeypatch.setattr(socket, "gethostbyname", resolver)
    monkeypatch.setattr(module, "in_container", lambda: False)
    resolve()
    assert resolver.calls == module.RESOLVE_ATTEMPTS


# --- what happens when it never resolves --------------------------------


def test_in_a_container_it_refuses_to_start(monkeypatch: pytest.MonkeyPatch):
    """127.0.0.1 is this container. Starting with it is starting broken.

    Refusing makes Docker restart the process until Postgres answers, which
    is the self-healing depends_on gives on `compose up` and withholds after a
    reboot.
    """
    monkeypatch.setattr(socket, "gethostbyname", Resolver(999))
    monkeypatch.setattr(module, "in_container", lambda: True)
    with pytest.raises(RuntimeError) as refusal:
        resolve()
    assert "127.0.0.1" in str(refusal.value)
    assert "container" in str(refusal.value)


def test_on_the_host_it_still_falls_back(monkeypatch: pytest.MonkeyPatch):
    """The fallback exists so scripts run on the NAS reach the published port.
    The fix must not take that away."""
    monkeypatch.setattr(socket, "gethostbyname", Resolver(999))
    monkeypatch.setattr(module, "in_container", lambda: False)
    assert resolve() == "postgresql+asyncpg://user:pass@127.0.0.1:5433/intraday_sentinel"


def test_the_published_port_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "gethostbyname", Resolver(999))
    monkeypatch.setattr(module, "in_container", lambda: False)
    monkeypatch.setenv("HOST_POSTGRES_PORT", "6543")
    assert resolve().endswith("@127.0.0.1:6543/intraday_sentinel")


def test_a_service_on_a_nondefault_port_rewrites_cleanly(monkeypatch: pytest.MonkeyPatch):
    """The old version replaced @postgres:5432, then @postgres: with a slash
    appended, so any other port produced @127.0.0.1:5433/5432/intraday_sentinel
    -- a URL naming a database that does not exist."""
    monkeypatch.setattr(socket, "gethostbyname", Resolver(999))
    monkeypatch.setattr(module, "in_container", lambda: False)
    rewritten = resolve("postgresql+asyncpg://user:pass@postgres:6000/intraday_sentinel")
    assert rewritten == "postgresql+asyncpg://user:pass@127.0.0.1:5433/intraday_sentinel"
    assert "5432" not in rewritten


# --- knowing where we are ------------------------------------------------


def test_the_docker_marker_file_is_believed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(module, "Path", lambda p: tmp_path / "dockerenv" if p == "/.dockerenv" else tmp_path / "none")
    (tmp_path / "dockerenv").write_text("")
    assert module.in_container() is True


def test_a_host_with_neither_signal_is_not_a_container(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(module, "Path", lambda _: tmp_path / "missing")
    assert module.in_container() is False


# --- how long the worker waits before handing the process back ----------


def test_twenty_failures_is_about_ten_minutes_of_getting_nowhere():
    """The worker exits after GIVE_UP_AFTER_FAILURES consecutive failures so
    the supervisor re-runs the startup that chose the address.

    The count only means anything alongside the backoff it counts, so both
    are pinned here: change the ceiling and the give-up time changes with it.
    """
    from app.services.worker_supervision import RestartBackoff
    from app.worker import GIVE_UP_AFTER_FAILURES

    backoff = RestartBackoff(maximum_seconds=30)
    waited = sum(backoff.record_failure(now=0) for _ in range(GIVE_UP_AFTER_FAILURES))

    assert backoff.failures == GIVE_UP_AFTER_FAILURES
    # Long enough to ride out a blip, a restarting Redis or a brief stall.
    assert waited > 5 * 60
    # Short enough that a structural fault costs minutes, not the four days
    # a worker once spent holding an address that could not work.
    assert waited < 15 * 60
