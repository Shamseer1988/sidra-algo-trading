"""Closing a live position on our terms, and every way that must not go wrong.

A stop is already resting at the broker when this runs. That single fact shapes
the whole module: a market exit sent while the stop is live risks both filling,
the exit flattening the position and the stop then opening one the other way.
So the stop is cancelled first, and a failed cancel sends nothing at all.

The tests that matter are therefore the refusals -- a cancel that failed, a
position with no trade behind it, a runtime that is not live -- and the one
asserting that an exit still works while the system is disarmed, because a
disarmed system with an open position is exactly the state this exists for.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import live_exit_manager as module
from app.services.trading_calendar import MarketPhase


class FakeAdapter:
    name = "UPSTOX"

    def __init__(self, positions, *, cancel_ok: bool = True, submit=None, resolved: bool = True) -> None:
        self._positions = positions
        self._cancel_ok = cancel_ok
        self._submit = submit or SimpleNamespace(
            status="ACCEPTED", broker_order_ids=["exit-1"], detail="", failure_code=None, failure_name=None
        )
        self._resolved = resolved
        self.cancelled: list[str] = []
        self.submitted: list = []

    async def normalised_positions(self):
        return self._positions

    async def cancel(self, broker_order_id: str):
        self.cancelled.append(broker_order_id)
        return (True, "") if self._cancel_ok else (False, "order already gone")

    async def describe(self, order):  # noqa: ANN001
        return SimpleNamespace(
            resolved=self._resolved, exchange="NSE_EQ", symbol="RVNL", detail="" if self._resolved else "no mapping"
        )

    async def submit(self, order, description):  # noqa: ANN001
        self.submitted.append(order)
        return self._submit


def position(net, symbol: str = "RVNL"):
    return SimpleNamespace(symbol=symbol, net_quantity=net, day_pnl=None, raw={})


def calendar(*, trading: bool = True, phase: str = "OPEN"):
    return SimpleNamespace(
        status_at=lambda _ts: SimpleNamespace(
            trading_day=trading, phase=getattr(MarketPhase, phase), reason="Regular session"
        )
    )


def settings(*, mode: str = "LIVE", enabled: bool = True):
    return SimpleNamespace(application_mode=mode, live_trading_enabled=enabled)


def signal(*, target: str = "2850.00", square_off: str | None = None, minutes_ago: int = 30):
    controls = {"exit_rules": {"square_off_time": square_off}} if square_off else {}
    return SimpleNamespace(
        id=uuid4(),
        instrument_token="NSE_EQ|INE415G01027",
        target_price=Decimal(target),
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        strategy_snapshot={"effective_controls": controls},
    )


def stop_row(product: str = "INTRADAY", numbers=("stop-1",)):
    """The real mapped class, for the reason the live failure gives.

    The exit builds its market order from this row's product. A stand-in with a
    plain ``product`` attribute cannot tell the broker's word from ours, which
    is the distinction that refused a live stop and the close behind it. The
    column holds "I"; the canonical value lives in the snapshot, as
    prepare_submission writes it.
    """
    from app.db.models import LiveOrderSubmission

    return LiveOrderSubmission(
        client_order_id=f"sidra-stop-{numbers[0] if numbers else 'x'}",
        broker="UPSTOX",
        exchange="NSE_EQ",
        trading_symbol="RVNL",
        product="I",
        price_type="SL-M",
        transaction_type="BUY",
        quantity=143,
        broker_order_numbers=list(numbers),
        request_snapshot={"canonical": {"product": product, "instrumentToken": "NSE_EQ|INE415G01027"}},
    )


class FakeSession:
    def __init__(self, *, sig=None, close=None, stops=None) -> None:
        self._signal = sig
        self._close = close
        self._stops = stops if stops is not None else [stop_row()]
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _v):
        return None

    async def get(self, _model, _pk):
        return self._signal


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(session=None, signal=signal(), close=Decimal("2900"), stops=[stop_row()])

    def session_factory():
        state.session = FakeSession(sig=state.signal, close=state.close, stops=state.stops)
        return state.session

    monkeypatch.setattr(module, "SessionLocal", session_factory)

    async def adapter_for(_settings, _session):
        return state.adapter

    async def find_signal(_session, _symbol, _start, _end):
        return state.signal

    async def latest_close(_session, _token):
        return state.close

    async def resting(_session, _signal_id, _start, _end):
        return state.stops

    async def prepare(_session, _request, _description, **kwargs):
        return SimpleNamespace(client_order_id=kwargs["client_order_id"], status="PREPARED")

    monkeypatch.setattr(module, "live_order_adapter", adapter_for)
    monkeypatch.setattr(module, "_signal_for", find_signal)
    monkeypatch.setattr(module, "_latest_close", latest_close)
    monkeypatch.setattr(module, "_resting_stops", resting)
    monkeypatch.setattr(module, "prepare_submission", prepare)
    monkeypatch.setattr(module, "apply_outcome", lambda *_a, **_k: None)
    return state


async def sweep(state, *, sett=None, cal=None):
    return await module.sweep_live_exits(sett or settings(), cal or calendar())


# --- the target --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_short_at_its_target_is_closed(wiring) -> None:
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2840")
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring)
    assert result.exits[0].acted is True
    assert wiring.adapter.submitted[0].side == "BUY"
    assert wiring.adapter.submitted[0].quantity == 143
    assert wiring.adapter.submitted[0].order_type == "MARKET"


@pytest.mark.asyncio
async def test_a_long_below_its_target_is_held(wiring) -> None:
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("50"))])
    result = await sweep(wiring)
    assert result.exits[0].acted is False
    assert result.exits[0].step == "holding"
    assert wiring.adapter.submitted == []
    assert wiring.adapter.cancelled == []


@pytest.mark.asyncio
async def test_the_stop_is_cancelled_before_the_exit_is_sent(wiring) -> None:
    """Both resting at once is how a flat position becomes a reversed one."""
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2840")
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    await sweep(wiring)
    assert wiring.adapter.cancelled == ["stop-1"]
    assert len(wiring.adapter.submitted) == 1


# --- the clock ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_position_past_its_square_off_is_closed_regardless_of_price(wiring) -> None:
    past = (datetime.now(UTC).astimezone(module.MARKET_TIMEZONE) - timedelta(minutes=5)).strftime("%H:%M")
    wiring.signal = signal(target="9999", square_off=past)
    wiring.close = Decimal("2840")  # nowhere near the target
    wiring.adapter = FakeAdapter([position(Decimal("50"))])
    result = await sweep(wiring)
    assert result.exits[0].acted is True
    assert "Square-off" in result.exits[0].reason


# --- the refusals ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_cancel_sends_no_exit(wiring) -> None:
    """The regression that matters: an exit beside a live stop can reverse you."""
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2840")
    wiring.adapter = FakeAdapter([position(Decimal("-143"))], cancel_ok=False)
    result = await sweep(wiring)
    assert result.exits[0].acted is False
    assert result.exits[0].step == "cancel_failed"
    assert wiring.adapter.submitted == []
    assert "by hand" in result.exits[0].detail


@pytest.mark.asyncio
async def test_a_position_with_no_trade_behind_it_is_left_alone(wiring) -> None:
    wiring.signal = None
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring)
    assert result.exits[0].step == "no_signal"
    assert wiring.adapter.submitted == []
    assert wiring.adapter.cancelled == []


@pytest.mark.asyncio
async def test_an_unreadable_position_is_reported_not_traded(wiring) -> None:
    wiring.adapter = FakeAdapter([position(None)])
    result = await sweep(wiring)
    assert result.exits[0].step == "unreadable"
    assert wiring.adapter.submitted == []


@pytest.mark.asyncio
async def test_a_flat_book_does_nothing(wiring) -> None:
    wiring.adapter = FakeAdapter([position(Decimal("0"))])
    result = await sweep(wiring)
    assert result.exits == []


@pytest.mark.asyncio
async def test_no_candle_means_no_target_decision(wiring) -> None:
    wiring.close = None
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring)
    assert result.exits[0].step == "no_price"
    assert wiring.adapter.submitted == []


@pytest.mark.asyncio
async def test_an_exit_that_fails_after_the_cancel_is_escalated(wiring) -> None:
    """The stop is gone and the exit did not land. The position is naked."""
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2840")
    wiring.adapter = FakeAdapter(
        [position(Decimal("-143"))],
        submit=SimpleNamespace(
            status="REJECTED", broker_order_ids=[], detail="no margin", failure_code="X", failure_name=None
        ),
    )
    result = await sweep(wiring)
    assert result.exits[0].step == "orphaned"
    assert "nothing behind it" in result.exits[0].detail


# --- when the sweep must not run ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "enabled"), [("PAPER", True), ("LIVE", False), ("REPLAY", True)])
async def test_a_runtime_that_is_not_live_sweeps_nothing(wiring, mode: str, enabled: bool) -> None:
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring, sett=settings(mode=mode, enabled=enabled))
    assert result.ran is False
    assert result.step == "runtime"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["CLOSED", "POST_MARKET"])
async def test_a_closed_exchange_sweeps_nothing(wiring, phase: str) -> None:
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring, cal=calendar(phase=phase))
    assert result.ran is False


@pytest.mark.asyncio
async def test_a_holiday_sweeps_nothing(wiring) -> None:
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring, cal=calendar(trading=False))
    assert result.ran is False


@pytest.mark.asyncio
async def test_an_unexpected_error_never_escapes(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("broker gone")

    monkeypatch.setattr(module, "live_order_adapter", boom)
    wiring.adapter = FakeAdapter([])
    result = await sweep(wiring)
    assert result.ran is False
    assert result.step == "broker"


# --- the property this module exists for -------------------------------------


def test_the_sweep_does_not_check_whether_the_system_is_armed() -> None:
    """Disarming stops new entries. A position already open must still close.

    Refusing to exit while disarmed would recreate the failure that started
    this: a live position the system opened and could not close.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "live_exit_manager.py").read_text()
    assert "current_activation" not in source
    assert "inspect_live_readiness" not in source


def test_an_exit_is_not_routed_through_the_entry_gates() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "live_exit_manager.py").read_text()
    assert "submit_live_order(" not in source


def test_the_position_is_matched_to_the_latest_entry_not_the_first() -> None:
    """A symbol can be traded more than once in a day.

    Four trades are allowed and one position is held at a time, so the position
    open now belongs to the most recent entry. Ordering the other way would
    manage the second trade against the first one's target and square-off.

    Asserted against the source rather than behaviour: _signal_for is one
    SQLAlchemy query, and a fake that answered it would be asserting on my own
    mock rather than on what Postgres returns. The ordering is the whole
    decision, so the ordering is what is pinned.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "live_exit_manager.py").read_text()
    body = source[source.index("async def _signal_for") : source.index("async def _resting_stops")]
    assert "created_at.desc()" in body
    assert "created_at.asc()" not in body


# --- matching a position to the entry that opened it ------------------------
#
# On 30-Sep this reported "BHARTIARTL - 7 open with no entry of ours behind it"
# once a minute, for a position it had opened itself minutes earlier. The match
# was a SQL equality between the position book's symbol and the submission's
# trading_symbol -- "BHARTIARTL" against "NSE_EQ|INE397D01024" -- so the entry
# was never found, the target and square-off were never applied, and the
# position went unmanaged.
#
# The test above pins the ordering by reading the source, because the match was
# inside a query no fake could answer honestly. Moving the match out of SQL is
# what makes these possible, and the absence of exactly this coverage is why
# the bug reached a live account.


class MatchSession:
    """Answers the day's submissions, then hands back the signal they name."""

    def __init__(self, submissions, signal) -> None:
        self._submissions = submissions
        self._signal = signal
        self.requested: list = []

    async def scalars(self, _statement):  # noqa: ANN001
        return list(self._submissions)

    async def get(self, _model, pk):  # noqa: ANN001
        self.requested.append(pk)
        return self._signal


def entry(symbol: str, token: str | None, signal_id: str = "sig-1"):
    return SimpleNamespace(trading_symbol=symbol, instrument_token=token, paper_signal_id=signal_id)


def held(symbol: str, token: str | None):
    from app.services.broker_adapter import BrokerPositionRecord

    return BrokerPositionRecord(symbol=symbol, net_quantity=Decimal("-7"), instrument_token=token)


BHARTI = "NSE_EQ|INE397D01024"

# Real bounds: the statement is still built for real, so a window of None would
# fail inside SQLAlchemy rather than exercising the match.
WINDOW = (datetime(2026, 9, 30, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_the_entry_is_found_when_the_broker_renames_the_instrument() -> None:
    """The regression: our own entry, reported as belonging to nobody."""
    wanted = object()
    session = MatchSession([entry(BHARTI, BHARTI)], wanted)
    found = await module._signal_for(session, held("BHARTIARTL", BHARTI), *WINDOW)
    assert found is wanted, "the entry that opened this position was not matched to it"


@pytest.mark.asyncio
async def test_a_position_from_another_instrument_finds_no_entry() -> None:
    """A wrong match would close a position against the wrong target."""
    session = MatchSession([entry(BHARTI, BHARTI)], object())
    found = await module._signal_for(session, held("RELIANCE", "NSE_EQ|INE002A01018"), *WINDOW)
    assert found is None


@pytest.mark.asyncio
async def test_the_most_recent_matching_entry_wins() -> None:
    """The query orders newest first; the match must take the first one it sees."""
    session = MatchSession(
        [entry(BHARTI, BHARTI, "newest"), entry(BHARTI, BHARTI, "older")],
        object(),
    )
    await module._signal_for(session, held("BHARTIARTL", BHARTI), *WINDOW)
    assert session.requested == ["newest"]


@pytest.mark.asyncio
async def test_a_broker_without_tokens_still_matches_on_symbol() -> None:
    """Firstock resolves real symbols; that path must keep working."""
    wanted = object()
    session = MatchSession([entry("RVNL", "NSE_EQ|INE415G01027")], wanted)
    found = await module._signal_for(session, held("RVNL", None), *WINDOW)
    assert found is wanted
