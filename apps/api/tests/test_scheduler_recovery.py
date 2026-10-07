"""The scheduled pass that settles submissions whose outcome was never learned.

``live_order_recovery`` could always answer the question -- it looks our own
client order id up in the broker's order book -- and until now nothing called it
except an operator clicking a button. On an unattended deployment that is not a
recovery path; it is a notification that trading has stopped, because the
readiness gate counts an unresolved submission as blocking. One ambiguous order
ended the trading day until somebody noticed.

Three things this pass must get right, and each is a way of making a bad
situation worse:

**It must not place anything.** It goes through the read-only report adapter. An
uncertain order resolved by a path that could submit is how one uncertain order
becomes two certain ones.

**A broker it cannot log in to has told it nothing.** Spending one of three
attempts on a login failure escalates a perfectly resolvable submission for a
reason that has nothing to do with the submission.

**An escalation has to reach a person.** NEEDS_REVIEW blocks live trading, and
that is the one thing an operator cannot discover from a container log.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import live_execution_gateway, live_order_recovery, scheduler


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def commit(self):
        self.commits += 1


def row(symbol: str = "RVNL", broker: str = "UPSTOX", status: str = "UNKNOWN"):
    return SimpleNamespace(
        trading_symbol=symbol,
        broker=broker,
        status=status,
        client_order_id="sidra-1",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )


def result(status: str, detail: str = "", numbers: list[str] | None = None):
    return live_order_recovery.RecoveryResult(status, detail, numbers or [])


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(
        rows=[row()],
        results=[result("RESOLVED_PLACED", "Found at the broker as 77.", ["77"])],
        adapter=SimpleNamespace(name="UPSTOX"),
        login_error=None,
        asked=[],
        announced=[],
        audited=[],
        before=None,
    )

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", FakeSession)

    async def pending(_session, *, before):
        state.before = before
        return state.rows

    async def resolve(adapter, record):  # noqa: ANN001
        state.asked.append((adapter, record))
        return state.results.pop(0) if state.results else result("UNKNOWN")

    async def report_adapter(_settings, _session, broker=None):  # noqa: ANN001
        if state.login_error is not None:
            raise state.login_error
        return state.adapter

    async def announce(_settings, escalated):  # noqa: ANN001
        state.announced.append(escalated)

    async def audit(event, metadata):  # noqa: ANN001
        state.audited.append((event, metadata))

    monkeypatch.setattr(live_order_recovery, "pending_resolution", pending)
    monkeypatch.setattr(live_order_recovery, "resolve_submission", resolve)
    monkeypatch.setattr(live_execution_gateway, "live_report_adapter", report_adapter)
    monkeypatch.setattr(scheduler, "_announce_recovery", announce)
    monkeypatch.setattr(scheduler, "_persist_audit", audit)
    return state


def settings(*, mode: str = "LIVE", enabled: bool = True):
    return SimpleNamespace(application_mode=mode, live_trading_enabled=enabled)


async def run(state, *, sett=None):
    await scheduler._make_submission_recovery_job(sett or settings())()
    return state


async def test_an_unknown_submission_is_looked_up_without_anyone_clicking(wiring) -> None:
    await run(wiring)
    assert len(wiring.asked) == 1
    assert wiring.announced == []


async def test_an_escalation_reaches_the_operator(wiring) -> None:
    """NEEDS_REVIEW stops live trading until a person acts."""
    wiring.results = [result("NEEDS_REVIEW", "Still absent from the order book after 3 attempts.")]
    await run(wiring)

    assert len(wiring.announced) == 1
    assert "RVNL" in wiring.announced[0][0]
    assert wiring.audited[0][0] == "scheduler.submission_escalated"


async def test_a_submission_that_resolves_itself_says_nothing(wiring) -> None:
    """An alert for every one of these is how an operator learns to stop
    reading them."""
    await run(wiring)
    assert wiring.announced == []
    assert wiring.audited == []


async def test_a_broker_we_cannot_log_in_to_costs_no_attempt(wiring) -> None:
    """A login failure has told us nothing about the order. Spending one of
    three attempts on it escalates a resolvable submission for a reason that
    has nothing to do with it."""
    wiring.login_error = RuntimeError("token expired")
    await run(wiring)

    assert wiring.asked == []
    assert wiring.announced == []


async def test_one_unreachable_broker_does_not_stop_the_others(wiring) -> None:
    class OnlyFirstFails:
        def __init__(self) -> None:
            self.calls = 0

    counter = OnlyFirstFails()
    wiring.rows = [row(symbol="RVNL", broker="FIRSTOCK"), row(symbol="TATASTEEL", broker="UPSTOX")]
    wiring.results = [result("RESOLVED_PLACED", "Found.", ["88"])]

    async def flaky(_settings, _session, broker=None):  # noqa: ANN001
        counter.calls += 1
        if counter.calls == 1:
            raise RuntimeError("firstock down")
        return wiring.adapter

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(live_execution_gateway, "live_report_adapter", flaky)
        await run(wiring)

    assert [record.trading_symbol for _adapter, record in wiring.asked] == ["TATASTEEL"]


async def test_nothing_open_asks_the_broker_nothing(wiring) -> None:
    wiring.rows = []
    await run(wiring)
    assert wiring.asked == []


@pytest.mark.parametrize(("mode", "enabled"), [("PAPER", True), ("LIVE", False), ("REPLAY", True)])
async def test_a_deployment_that_is_not_live_resolves_nothing(wiring, mode: str, enabled: bool) -> None:
    await run(wiring, sett=settings(mode=mode, enabled=enabled))
    assert wiring.asked == []
    assert wiring.before is None


async def test_a_submission_still_in_flight_is_not_asked_about(wiring) -> None:
    """A PREPARED row is written one line before the order is sent. Looking
    immediately would ask the broker about an order that has not been placed
    yet, and count the miss against a budget of three."""
    await run(wiring)

    assert wiring.before is not None
    gap = (datetime.now(UTC) - wiring.before).total_seconds()
    assert gap >= scheduler.RESOLUTION_SETTLE_SECONDS
