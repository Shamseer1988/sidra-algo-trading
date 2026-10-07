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

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services import live_exit_manager as module
from app.services.trading_calendar import MarketPhase


class FakeAdapter:
    name = "UPSTOX"

    def __init__(self, positions, *, cancel_ok: bool = True, submit=None, resolved: bool = True, orders=None) -> None:
        self._positions = positions
        # Our stop, working at the broker. The default, because that is the
        # normal state of a held position and the sweep now checks for it.
        self._orders = working_book() if orders is None else orders
        self._cancel_ok = cancel_ok
        self._submit = submit or SimpleNamespace(
            status="ACCEPTED", broker_order_ids=["exit-1"], detail="", failure_code=None, failure_name=None
        )
        self._resolved = resolved
        self.cancelled: list[str] = []
        self.submitted: list = []

    async def normalised_positions(self):
        return self._positions

    async def normalised_orders(self):
        if isinstance(self._orders, Exception):
            raise self._orders
        return self._orders

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


def book_order(broker_order_id: str = "stop-1", status: str = "OPEN"):
    """A real BrokerOrderRecord, not a stand-in.

    The sweep reads ``status`` and ``broker_order_id`` off these, and a
    SimpleNamespace would answer for a field that does not exist -- which is
    the shape of fake behind two earlier live failures on this path.
    """
    from app.services.broker_adapter import BrokerOrderRecord

    return BrokerOrderRecord(
        broker_order_id=broker_order_id,
        client_order_id="sidra-stop-stop-1",
        status=status,
        symbol="RVNL",
        side="BUY",
        order_type="SL-M",
        quantity=143,
        filled_quantity=0,
    )


def working_book():
    return [book_order()]


def position(net, symbol: str = "RVNL", token: str = "NSE_EQ|INE415G01027"):
    """A real BrokerPositionRecord, for the same reason ``book_order`` is real.

    The sweep now asks a position whether a book row belongs to it, and a
    SimpleNamespace answers for ``identifies`` by raising rather than by
    matching -- which is the shape of fake that let two earlier live failures
    through on this path.
    """
    from app.services.broker_adapter import BrokerPositionRecord

    return BrokerPositionRecord(symbol=symbol, net_quantity=net, day_pnl=None, instrument_token=token, raw={})


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


@contextmanager
def _at_ist(hour: int, minute: int):
    """Run the sweep as if the exchange clock read this.

    The square-off is an exchange-time decision, so a test that used the real
    clock would pass or fail depending on what time of day it ran -- and the
    one failure it is guarding against happened at ten past three.
    """
    real = module.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            moment = real(2026, 10, 6, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
            return moment.astimezone(tz) if tz is not None else moment

    module.datetime = Frozen
    try:
        yield
    finally:
        module.datetime = real


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
    state = SimpleNamespace(
        session=None,
        signal=signal(),
        close=Decimal("2900"),
        # How old the last completed candle is. Fresh by default; the target is
        # only measured against a price recent enough to still be true.
        close_age_minutes=1.0,
        stops=[stop_row()],
        deadline=None,
    )

    def session_factory():
        state.session = FakeSession(sig=state.signal, close=state.close, stops=state.stops)
        return state.session

    monkeypatch.setattr(module, "SessionLocal", session_factory)

    async def adapter_for(_settings, _session):
        return state.adapter

    async def find_signal(_session, _symbol, _start, _end):
        return state.signal

    async def latest_close(_session, _token):
        if state.close is None:
            return None, None
        return state.close, module.datetime.now(UTC) - timedelta(minutes=state.close_age_minutes)

    async def resting(_session, _signal_id, _start, _end):
        return state.stops

    async def prepare(_session, _request, _description, **kwargs):
        return SimpleNamespace(client_order_id=kwargs["client_order_id"], status="PREPARED")

    async def deadline(_session):
        return state.deadline

    monkeypatch.setattr(module, "_account_deadline", deadline)
    monkeypatch.setattr(module, "live_order_adapter", adapter_for)
    monkeypatch.setattr(module, "_signal_for", find_signal)
    monkeypatch.setattr(module, "_latest_close", latest_close)
    monkeypatch.setattr(module, "_recorded_stops", resting)
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


# --- the position is still protected -----------------------------------------
#
# ``protect_after_fill`` fires once, in the seconds after the entry. For a long
# time that was the only attempt this system ever made, so a stop refused at
# 09:37 left the position naked until the square-off and nothing scheduled
# would notice. On 5 October a ₹100 planned risk reached ₹163 and climbing on a
# position whose stop had simply been rejected for an invalid tick price.


@pytest.mark.asyncio
async def test_a_held_position_with_no_working_stop_is_protected(wiring, monkeypatch) -> None:
    calls = []

    async def spy(_session, _adapter, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(protected=True, flattened=False, detail="Stop at 2850.00.")

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    # The book holds nothing of ours: the stop was rejected at placement.
    wiring.adapter = FakeAdapter([position(Decimal("50"))], orders=[])

    result = await sweep(wiring)

    assert len(calls) == 1
    assert calls[0]["net"] == Decimal("50")
    assert calls[0]["signal"] is wiring.signal
    assert result.exits[0].step == "unprotected"
    assert "no working stop behind it" in result.exits[0].detail


@pytest.mark.asyncio
async def test_a_stop_the_broker_has_finished_with_does_not_count(wiring, monkeypatch) -> None:
    """Our table says what we were told at placement. Only the book says whether
    the order is working now -- a stop cancelled or rejected after the fact
    leaves a row that still reads ACCEPTED."""
    calls = []

    async def spy(_session, _adapter, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(protected=True, flattened=False, detail="Stop re-placed.")

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("50"))], orders=[book_order(status="REJECTED")])

    await sweep(wiring)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_working_stop_is_left_exactly_alone(wiring, monkeypatch) -> None:
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("50"))])

    result = await sweep(wiring)

    assert calls == []
    assert result.exits[0].step == "holding"
    assert wiring.adapter.submitted == []


@pytest.mark.asyncio
async def test_an_unreadable_order_book_places_no_second_stop(wiring, monkeypatch) -> None:
    """A book we cannot read is not a book with no stops in it.

    Treating it as empty would put a second stop behind a position that already
    has one. Both would fill, and the position would end up reversed rather
    than flat -- the one outcome this module exists to prevent.
    """
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("50"))], orders=RuntimeError("connection reset"))

    result = await sweep(wiring)

    assert calls == []
    assert result.exits[0].step == "holding"


@pytest.mark.asyncio
async def test_a_position_at_its_target_exits_rather_than_being_re_stopped(wiring, monkeypatch) -> None:
    """The protection check must not divert a trade that should be closing."""
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2840")
    wiring.adapter = FakeAdapter([position(Decimal("-143"))], orders=[])

    result = await sweep(wiring)

    assert calls == []
    assert result.exits[0].acted is True
    assert wiring.adapter.submitted[0].order_type == "MARKET"


@pytest.mark.asyncio
async def test_the_account_deadline_closes_a_position_the_strategy_would_have_held(wiring) -> None:
    """The regression, end to end.

    A position carried 15:15 from the morning's settings while the operator had
    since moved the account to 15:00. Nothing fired at 15:00, Upstox refused a
    protective stop at 15:10, and the position was closed by hand.
    """
    wiring.signal = signal(target="2900", square_off="15:15")
    wiring.close = Decimal("2880")  # nowhere near the target
    wiring.deadline = "15:00"
    wiring.adapter = FakeAdapter([position(Decimal("50"))])

    with _at_ist(15, 5):
        result = await sweep(wiring)

    assert result.exits[0].acted is True
    assert "15:00" in result.exits[0].reason
    assert wiring.adapter.submitted[0].order_type == "MARKET"


@pytest.mark.asyncio
async def test_a_strategy_that_closes_earlier_keeps_its_own_time(wiring) -> None:
    """A ceiling, not an override. 14:30 is a strategy's decision and stands."""
    wiring.signal = signal(target="2900", square_off="14:30")
    wiring.close = Decimal("2880")
    wiring.deadline = "15:00"
    wiring.adapter = FakeAdapter([position(Decimal("50"))])

    with _at_ist(14, 45):
        result = await sweep(wiring)

    assert result.exits[0].acted is True
    assert "14:30" in result.exits[0].reason


@pytest.mark.asyncio
async def test_an_unprotected_position_near_the_deadline_is_closed_not_stopped(wiring, monkeypatch) -> None:
    """A stop with three minutes to live protects almost nothing, would be
    cancelled by the square-off about to run, and is refused by the broker near
    the close anyway -- "the Intraday Order window for the segment is currently
    closed for the day"."""
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900", square_off="15:00")
    wiring.close = Decimal("2880")
    wiring.deadline = "15:00"
    wiring.adapter = FakeAdapter([position(Decimal("50"))], orders=[])

    with _at_ist(14, 57):
        result = await sweep(wiring)

    assert calls == []
    assert result.exits[0].step == "unprotected_closing"
    assert wiring.adapter.submitted[0].order_type == "MARKET"


@pytest.mark.asyncio
async def test_an_unprotected_position_early_in_the_day_still_gets_a_stop(wiring, monkeypatch) -> None:
    """The watchdog must not become a hair trigger that closes every position
    that briefly loses its stop at eleven in the morning."""
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)
        return SimpleNamespace(protected=True, flattened=False, detail="Stop at 2850.00.")

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.signal = signal(target="2900", square_off="15:00")
    wiring.close = Decimal("2880")
    wiring.deadline = "15:00"
    wiring.adapter = FakeAdapter([position(Decimal("50"))], orders=[])

    with _at_ist(11, 0):
        result = await sweep(wiring)

    assert len(calls) == 1
    assert result.exits[0].step == "unprotected"
    assert wiring.adapter.submitted == []


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
    """No price, no target decision -- but the stop is still checked.

    This branch used to return before the protection watchdog ran, so a
    position whose instrument had stopped producing candles was the one
    position nothing ever looked at. The watchdog lives inside
    _hold_or_protect and nothing else calls it.
    """
    wiring.close = None
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])
    result = await sweep(wiring)
    assert result.exits[0].step == "stale_price"
    assert result.exits[0].acted is False
    assert wiring.adapter.submitted == []
    assert "no completed candle" in result.exits[0].detail
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
    body = source[source.index("async def _signal_for") : source.index("async def _recorded_stops")]
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


def entry(symbol: str, token: str | None, signal_id: str = "sig-1", order_type: str = "MARKET"):
    """A real row. The third helper in this suite to need converting.

    Each stand-in answered for whatever the code reached for, so none of them
    could show that `instrument_token`, `canonical_product` or
    `canonical_order_type` read from a snapshot rather than a column.
    """
    from app.db.models import LiveOrderSubmission

    return LiveOrderSubmission(
        client_order_id=f"sidra-{signal_id}",
        paper_signal_id=signal_id,
        broker="UPSTOX",
        exchange="NSE_EQ",
        trading_symbol=symbol,
        product="I",
        price_type=order_type,
        transaction_type="SELL",
        quantity=7,
        request_snapshot={"canonical": {"instrumentToken": token, "product": "INTRADAY", "orderType": order_type}},
    )


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


# --- the three failures the live-path review found ---------------------------


@pytest.mark.asyncio
async def test_a_stop_already_cancelled_does_not_block_the_next_exit(wiring) -> None:
    """The square-off has to be able to try twice.

    Nothing ever writes a submission back to cancelled, so a stop cancelled at
    15:00 was still handed back as resting at 15:01. The cancel was re-sent, the
    broker refused it -- an order that is already gone cannot be cancelled --
    and that refusal was read as "a stop may still be live", so no exit was
    sent. Every minute after that did the same. The one case the square-off
    exists for, an exit that did not go through, was the case in which it gave
    up.
    """
    wiring.adapter = FakeAdapter([position(Decimal("143"))], orders=[book_order(status="CANCELLED")])

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        result = await sweep(wiring)

    # Nothing was asked of the broker, because nothing was resting.
    assert wiring.adapter.cancelled == []
    assert result.exits[0].step == "exited"
    assert len(wiring.adapter.submitted) == 1


@pytest.mark.asyncio
async def test_a_cancel_refused_for_an_order_that_is_already_gone_lets_the_exit_through(wiring) -> None:
    """A cancellation fails for two quite different reasons, and only one of
    them is a reason to hold the exit back. The book settles it, not the
    broker's error text."""

    class VanishingAdapter(FakeAdapter):
        """Open on the first read, gone once the cancel has been refused."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._asked = False

        async def normalised_orders(self):
            return [book_order(status="OPEN" if not self._asked else "CANCELLED")]

        async def cancel(self, broker_order_id: str):
            self._asked = True
            self.cancelled.append(broker_order_id)
            return False, "order is not open"

    wiring.adapter = VanishingAdapter([position(Decimal("143"))])

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        result = await sweep(wiring)

    assert wiring.adapter.cancelled == ["stop-1"]
    assert result.exits[0].step == "exited"


@pytest.mark.asyncio
async def test_a_cancel_the_broker_really_refuses_still_sends_no_exit(wiring) -> None:
    """The protection the re-check must not remove: a stop that is genuinely
    still working is a stop that would fill beside the exit."""
    wiring.adapter = FakeAdapter([position(Decimal("143"))], cancel_ok=False)

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        result = await sweep(wiring)

    assert result.exits[0].step == "cancel_failed"
    assert wiring.adapter.submitted == []


@pytest.mark.asyncio
async def test_an_order_we_never_recorded_is_cleared_before_the_exit(wiring) -> None:
    """A stop placed by hand, or one whose send returned UNKNOWN, is not in our
    tables. It survived a cancellation that looked only at our own rows, filled
    beside the market exit, and left the position reversed rather than flat."""
    wiring.stops = []
    wiring.adapter = FakeAdapter(
        [position(Decimal("143"))],
        orders=[book_order(broker_order_id="placed-by-hand", status="OPEN")],
    )

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        result = await sweep(wiring)

    assert wiring.adapter.cancelled == ["placed-by-hand"]
    assert result.exits[0].step == "exited"


def _book_order_for(symbol: str, broker_order_id: str):
    from app.services.broker_adapter import BrokerOrderRecord

    return BrokerOrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        status="OPEN",
        symbol=symbol,
        side="SELL",
        order_type="SL-M",
        quantity=10,
        filled_quantity=0,
    )


@pytest.mark.asyncio
async def test_a_resting_order_on_another_instrument_is_left_alone(wiring) -> None:
    """Clearing the book for this position must not reach into another one."""
    wiring.stops = []
    wiring.adapter = FakeAdapter(
        [position(Decimal("143"))],
        orders=[_book_order_for("TATASTEEL", "somebody-elses")],
    )

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        await sweep(wiring)

    assert wiring.adapter.cancelled == []


@pytest.mark.asyncio
async def test_a_stop_covering_only_part_of_the_position_is_topped_up(wiring, monkeypatch) -> None:
    """A partially filled entry leaves a stop for what filled and a resting
    remainder that fills later. The watchdog checked that a stop existed rather
    than that it covered anything, so a stop for 1 behind a position of 2 read
    as protected -- once a minute, all day."""
    calls = []

    async def spy(_session, _adapter, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(protected=True, flattened=False, detail="Stop at 2850.00 for the rest.")

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.stops = [stop_row()]
    wiring.stops[0].quantity = 100
    wiring.signal = signal(target="2900")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("143"))])

    result = await sweep(wiring)

    # The uncovered part only, and on the side the position is held.
    assert len(calls) == 1
    assert calls[0]["net"] == Decimal("43")
    assert "covering only 100" in result.exits[0].detail


@pytest.mark.asyncio
async def test_a_short_tops_up_on_the_side_it_is_held(wiring, monkeypatch) -> None:
    calls = []

    async def spy(_session, _adapter, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(protected=True, flattened=False, detail="")

    monkeypatch.setattr(module, "protect_position", spy)
    wiring.stops = [stop_row()]
    wiring.stops[0].quantity = 2
    wiring.signal = signal(target="2800")
    wiring.close = Decimal("2880")
    wiring.adapter = FakeAdapter([position(Decimal("-5"))])

    await sweep(wiring)
    assert calls[0]["net"] == Decimal("-3")


@pytest.mark.asyncio
async def test_a_part_covered_position_near_the_deadline_clears_its_stop_before_closing(wiring) -> None:
    """Closing the whole position beside a stop that still covers part of it is
    how a flat intention becomes a reversed position."""
    wiring.stops = [stop_row()]
    wiring.stops[0].quantity = 100

    with _at_ist(14, 58):
        wiring.signal = signal(target="2900", square_off="15:00")
        wiring.close = Decimal("2880")
        wiring.adapter = FakeAdapter([position(Decimal("143"))])
        result = await sweep(wiring)

    assert wiring.adapter.cancelled == ["stop-1"]
    assert result.exits[0].step == "unprotected_closing"
    assert len(wiring.adapter.submitted) == 1


@pytest.mark.asyncio
async def test_a_part_covered_position_near_the_deadline_sends_nothing_if_the_stop_will_not_clear(wiring) -> None:
    wiring.stops = [stop_row()]
    wiring.stops[0].quantity = 100

    with _at_ist(14, 58):
        wiring.signal = signal(target="2900", square_off="15:00")
        wiring.close = Decimal("2880")
        wiring.adapter = FakeAdapter([position(Decimal("143"))], cancel_ok=False)
        result = await sweep(wiring)

    assert result.exits[0].step == "cancel_failed"
    assert wiring.adapter.submitted == []


# --- a price too old to decide on --------------------------------------------
#
# The target was measured against the newest candle whatever its age. A feed
# that stopped at 11:00 left the 14:30 target judged against an 11:00 close,
# which is how a target is missed on a trade that reached it, or hit on one
# that did not.


@pytest.mark.asyncio
async def test_a_target_is_not_judged_against_a_price_from_hours_ago(wiring) -> None:
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2800")  # through the target of a short
    wiring.close_age_minutes = 45.0
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])

    result = await sweep(wiring)

    assert result.exits[0].step == "stale_price"
    assert wiring.adapter.submitted == []
    assert "45 minutes old" in result.exits[0].detail


@pytest.mark.asyncio
async def test_a_recent_candle_still_decides_the_target(wiring) -> None:
    """A minute with no trade in it produces no candle at all, so a small gap is
    ordinary and must not stop a target being taken."""
    wiring.signal = signal(target="2850")
    wiring.close = Decimal("2800")
    wiring.close_age_minutes = module.TARGET_PRICE_MAX_AGE_MINUTES - 1
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])

    result = await sweep(wiring)

    assert result.exits[0].step == "exited"
    assert len(wiring.adapter.submitted) == 1


@pytest.mark.asyncio
async def test_a_stale_price_still_gets_the_position_a_stop(wiring, monkeypatch) -> None:
    calls = []

    async def spy(_session, _adapter, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(protected=True, flattened=False, detail="Stop at 2850.00.")

    monkeypatch.setattr(module, "protect_position", spy)
    # Through the target of a short, and far too old to act on. Trusted, this
    # price would close the trade; the stop is what has to happen instead.
    wiring.close = Decimal("2800")
    wiring.close_age_minutes = 45.0
    # Nothing of ours on the book: the stop was rejected at placement.
    wiring.adapter = FakeAdapter([position(Decimal("-143"))], orders=[])

    result = await sweep(wiring)

    assert len(calls) == 1
    assert result.exits[0].step == "unprotected"


@pytest.mark.asyncio
async def test_the_square_off_does_not_wait_for_a_price(wiring) -> None:
    """The clock is not a price question. A dead feed must not keep a position
    open past the deadline."""
    wiring.close = None

    with _at_ist(15, 2):
        wiring.signal = signal(square_off="15:00")
        wiring.adapter = FakeAdapter([position(Decimal("-143"))])
        result = await sweep(wiring)

    assert result.exits[0].step == "exited"


@pytest.mark.asyncio
async def test_a_stale_price_is_reported_rather_than_left_in_the_quiet_pile(wiring) -> None:
    wiring.close_age_minutes = 45.0
    wiring.adapter = FakeAdapter([position(Decimal("-143"))])

    result = await sweep(wiring)

    assert [item.step for item in result.noteworthy] == ["stale_price"]
