"""Per-order live authorisation.

Only one outcome here is dangerous: authorising something that should have been
refused. These tests are weighted accordingly — the happy path is a single case,
and every other test proves a refusal.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS
from app.db.models import ExecutionReconciliation, SessionHalt
from app.services.broker_adapter import BrokerOrder, BrokerOrderDescription, FirstockAdapter
from app.services.firstock.orders import FirstockApiError, FirstockTransportUnknown
from app.services.live_risk import RECONCILIATION_MAX_AGE, authorize_live_order

ORDER = BrokerOrder(
    instrument_token="NSE_EQ|INE669E01016",
    side="BUY",
    quantity=1,
    order_type="LIMIT",
    product="DELIVERY",
    price=Decimal("418"),
    client_order_id="sidra-test",
)

# Supplied rather than resolved: the risk engine is handed a description by the
# execution path, and resolving one here would drag the symbol map into tests
# about margin and reconciliation.
DESCRIPTION = BrokerOrderDescription(
    True,
    exchange="NSE",
    symbol="IDEA-EQ",
    side="B",
    order_type="LMT",
    product="C",
    validity="DAY",
)


class FakeSession:
    """Answers by entity, not by call order.

    A fake that returned the same object to every ``scalar`` handed the
    reconciliation record back when the daily-limit gate asked for a halt, and
    the gate then refused every order with a reason taken from the wrong table.
    Dispatching on the queried entity makes that impossible rather than unlikely.
    """

    def __init__(
        self,
        reconciliation: object | None = None,
        halt: object | None = None,
        controls=None,  # noqa: ANN001
        allocated: list[tuple[object, object, Decimal]] | None = None,
        controls_raise: Exception | None = None,
    ) -> None:
        self._by_entity = {ExecutionReconciliation: reconciliation, SessionHalt: halt}
        self._controls = controls
        self._controls_raise = controls_raise
        # One row per live submission the day already carries: (signal id, the
        # risk that submission was sized from).
        self._allocated = allocated or []
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, query: object) -> object | None:
        entity = query.column_descriptions[0]["entity"]
        return self._by_entity.get(entity)

    async def get(self, _entity: object, _key: object) -> object | None:
        if self._controls_raise is not None:
            raise self._controls_raise
        if self._controls is None:
            return None
        return SimpleNamespace(value=self._controls)

    async def execute(self, _query: object) -> object:
        return SimpleNamespace(all=lambda: list(self._allocated))

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def reconciliation(*, safe: bool = True, age: timedelta = timedelta(minutes=1)) -> SimpleNamespace:
    return SimpleNamespace(
        safe_to_trade=safe,
        detail="blocked: untracked broker order" if not safe else "clean",
        created_at=datetime.now(UTC) - age,
    )


class FakeClient:
    def __init__(
        self,
        margin: dict | None = None,
        raises: Exception | None = None,
        positions: list[dict] | None = None,
        positions_raise: Exception | None = None,
    ) -> None:
        self._margin = margin if margin is not None else {"availableMargin": "50000", "marginOnNewOrder": "418"}
        self._raises = raises
        self._positions = positions if positions is not None else []
        self._positions_raise = positions_raise

    async def order_margin(self, **_kwargs: object) -> dict:
        if self._raises:
            raise self._raises
        return self._margin

    async def position_book(self) -> list[dict]:
        if self._positions_raise:
            raise self._positions_raise
        return self._positions


def readiness(ready: bool):
    """Stand in for inspect_live_readiness, which is closed by construction today."""

    async def _inspect(_session: object, _settings: object) -> SimpleNamespace:
        return SimpleNamespace(
            overall_ready=ready,
            gates=[SimpleNamespace(key="broker_adapter", passed=ready)],
        )

    return _inspect


def check(decision, key: str):
    return next(item for item in decision.checks if item.key == key)


# Distinguishes "caller said nothing" from "caller said there is no record",
# since None is itself a meaningful value for recon.
_UNSET = object()


def limits(**overrides) -> dict:
    """Trading controls with the daily limits off unless a test turns them on."""
    return {**DEFAULT_TRADING_CONTROLS, "daily_loss_limit": 0.0, "daily_profit_target": 0.0, **overrides}


async def authorize(
    monkeypatch,
    *,
    ready=True,
    mode="AUTOMATIC",
    recon=_UNSET,
    client=None,
    description=DESCRIPTION,
    halt=None,
    controls=None,
    record_halt=False,
    session=None,
    allocated=None,
    candidate_risk=None,
    **overrides,
):
    monkeypatch.setattr("app.services.live_risk.inspect_live_readiness", readiness(ready))
    return await authorize_live_order(
        session
        or FakeSession(
            reconciliation() if recon is _UNSET else recon,
            halt,
            controls or limits(),
            allocated=allocated,
        ),
        SimpleNamespace(),
        # The real adapter over a fake client: the margin comparison is the part
        # worth not mocking. Its session argument is only used for symbol
        # translation, which a supplied description has already done.
        FirstockAdapter(client or FakeClient(), None),
        approval_mode=mode,
        order=dataclasses.replace(ORDER, **overrides) if overrides else ORDER,
        description=description,
        record_halt=record_halt,
        candidate_risk=candidate_risk,
    )


async def test_every_gate_passing_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch)
    assert decision.authorized is True
    assert decision.reason == "Authorised"
    assert decision.failures == []


async def test_readiness_gate_alone_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the state the repository ships in, and it must deny."""
    decision = await authorize(monkeypatch, ready=False)
    assert decision.authorized is False
    assert check(decision, "live_readiness").passed is False


@pytest.mark.parametrize("mode", ["DISABLED", "", "disabled", "something-else"])
async def test_only_the_two_documented_modes_permit_submission(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    decision = await authorize(monkeypatch, mode=mode)
    assert decision.authorized is False
    assert check(decision, "approval_mode").passed is False


@pytest.mark.parametrize("mode", ["TELEGRAM_APPROVAL", "AUTOMATIC", "  automatic  "])
async def test_permitted_modes_are_case_and_whitespace_tolerant(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    decision = await authorize(monkeypatch, mode=mode)
    assert check(decision, "approval_mode").passed is True


async def test_no_reconciliation_ever_recorded_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, recon=None)
    assert decision.authorized is False
    assert check(decision, "reconciliation").passed is False


async def test_blocked_reconciliation_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, recon=reconciliation(safe=False))
    assert decision.authorized is False
    assert "untracked broker order" in check(decision, "reconciliation").detail


async def test_stale_reconciliation_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass from an hour ago describes an account that may since have changed."""
    stale = reconciliation(age=RECONCILIATION_MAX_AGE + timedelta(minutes=1))
    decision = await authorize(monkeypatch, recon=stale)
    assert decision.authorized is False
    assert "old" in check(decision, "reconciliation").detail


async def test_naive_reconciliation_timestamp_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres can hand back a naive datetime; comparing it must not raise."""
    naive = SimpleNamespace(
        safe_to_trade=True,
        detail="clean",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    decision = await authorize(monkeypatch, recon=naive)
    assert check(decision, "reconciliation").passed is True


async def test_insufficient_broker_margin_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient({"availableMargin": "100", "marginOnNewOrder": "418"})
    decision = await authorize(monkeypatch, client=client)
    assert decision.authorized is False
    assert check(decision, "broker_margin").passed is False


async def test_success_envelope_with_insufficient_remark_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented response reports shortfalls in remarks, not in the status."""
    client = FakeClient({"availableMargin": "50000", "marginOnNewOrder": "418", "remarks": "Insufficient balance"})
    decision = await authorize(monkeypatch, client=client)
    assert decision.authorized is False
    assert "Insufficient balance" in check(decision, "broker_margin").detail


async def test_unreadable_margin_refuses_rather_than_assuming_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient({"availableMargin": "n/a", "marginOnNewOrder": "418"})
    decision = await authorize(monkeypatch, client=client)
    assert decision.authorized is False
    assert check(decision, "broker_margin").passed is False


@pytest.mark.parametrize(
    "error",
    [FirstockTransportUnknown("timeout"), FirstockApiError("bad", code="400", name="BAD_REQUEST", field="x")],
)
async def test_margin_call_failure_refuses(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    decision = await authorize(monkeypatch, client=FakeClient(raises=error))
    assert decision.authorized is False
    assert check(decision, "broker_margin").passed is False


@pytest.mark.parametrize("quantity", ["0", "-5", "", "abc", "1.5", None])
async def test_non_positive_or_unparseable_quantity_refuses(monkeypatch: pytest.MonkeyPatch, quantity: object) -> None:
    """A risk engine that raises is a risk engine that cannot say no."""
    decision = await authorize(monkeypatch, quantity=quantity)
    assert decision.authorized is False
    assert check(decision, "quantity").passed is False


async def test_an_instrument_that_cannot_be_named_refuses_as_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reported as an instrument problem, not folded into the margin check.

    Folded together, somebody would go and look at the account balance for a
    symbol the broker has simply never heard of.
    """
    decision = await authorize(
        monkeypatch,
        description=BrokerOrderDescription(False, detail="No verified mapping for NSE_EQ|INE669E01016"),
    )
    assert decision.authorized is False
    assert check(decision, "instrument").passed is False
    assert check(decision, "broker_margin").passed is False


async def test_all_checks_run_so_an_operator_sees_every_objection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = await authorize(
        monkeypatch,
        ready=False,
        mode="DISABLED",
        recon=None,
        client=FakeClient({"availableMargin": "1", "marginOnNewOrder": "9999"}),
        quantity="0",
    )
    assert decision.authorized is False
    assert {item.key for item in decision.failures} == {
        "live_readiness",
        "approval_mode",
        "reconciliation",
        "quantity",
        "broker_margin",
    }


async def test_snapshot_is_serialisable_for_the_audit_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    decision = await authorize(monkeypatch, ready=False)
    assert json.loads(json.dumps(decision.snapshot()))["authorized"] is False


# --- the daily limit, measured at the broker -----------------------------
#
# The same two numbers the operator set for paper, read from a different source.
# Paper P&L is a simulation, and a real order refused because a simulation had a
# good morning is a refusal nobody could act on.


def position(symbol: str = "IDEA-EQ", *, total: str | None = None, **extra) -> dict:
    record = {"tradingSymbol": symbol, "netQuantity": "10", **extra}
    if total is not None:
        record["totalPNL"] = total
    return record


async def test_no_configured_limit_leaves_the_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero disables it, exactly as it does for paper."""
    decision = await authorize(monkeypatch, controls=limits())
    assert check(decision, "daily_limit").passed is True
    assert decision.authorized is True


async def test_a_day_inside_its_limits_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions=[position(total="500")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_profit_target=2000.0))
    assert check(decision, "daily_limit").passed is True


async def test_the_brokers_profit_reaching_the_target_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions=[position(total="2000")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_profit_target=2000.0))
    assert decision.authorized is False
    assert "Daily profit target reached" in check(decision, "daily_limit").detail


async def test_the_brokers_loss_reaching_the_limit_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions=[position(total="-1000")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert "Daily loss limit reached" in check(decision, "daily_limit").detail


async def test_every_position_counts_towards_the_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """One winner does not cancel the day; the account's total does."""
    client = FakeClient(positions=[position("A", total="-1400"), position("B", total="300")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert check(decision, "daily_limit").data["session_pnl"] == "-1100"


async def test_realised_and_unrealised_are_summed_when_there_is_no_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firstock's reference names two spellings for the unrealised half."""
    client = FakeClient(positions=[position(RealizedPNL="-600", unrealizedMTOM="-500")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert check(decision, "daily_limit").data["session_pnl"] == "-1100"


async def test_the_other_unrealised_spelling_is_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions=[position(RealizedPNL="-600", totalMTM="-500")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert check(decision, "daily_limit").data["session_pnl"] == "-1100"


# --- fail closed ----------------------------------------------------------


async def test_unreadable_position_pnl_refuses_rather_than_counting_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A day whose total cannot be established is a day that cannot be bounded.

    Counting it as zero would let the limit pass on exactly the position it
    could not see.
    """
    client = FakeClient(positions=[position(total="-200"), position("MYSTERY")])
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert "MYSTERY" in check(decision, "daily_limit").detail


async def test_a_broker_that_will_not_answer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions_raise=FirstockTransportUnknown("positionBook timed out"))
    decision = await authorize(monkeypatch, client=client, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert check(decision, "daily_limit").passed is False


# --- the latch ------------------------------------------------------------


async def test_a_recorded_halt_refuses_without_asking_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The day is already over; a network call cannot change that.

    And it must refuse even though the account has since recovered — which is
    the whole point of latching.
    """
    recovered = FakeClient(positions=[position(total="0")])
    halt = SimpleNamespace(reason="Daily loss limit reached", session_pnl=Decimal("-1200"))
    decision = await authorize(monkeypatch, client=recovered, halt=halt, controls=limits(daily_loss_limit=1000.0))
    assert decision.authorized is False
    assert "-1200" in check(decision, "daily_limit").detail


async def test_the_submission_path_records_the_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(reconciliation(), None, limits(daily_loss_limit=1000.0))
    client = FakeClient(positions=[position(total="-1500")])
    await authorize(monkeypatch, client=client, session=session, record_halt=True)
    assert [type(item).__name__ for item in session.added] == ["SessionHalt"]
    assert session.added[0].mode == "LIVE"
    assert session.added[0].reason == "Daily loss limit reached"


async def test_a_path_that_cannot_submit_does_not_close_the_live_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow evaluator calls this engine and submits nothing.

    It still sees the refusal, so its evidence is honest, but it must not be
    able to halt live trading on the strength of an evaluation.
    """
    session = FakeSession(reconciliation(), None, limits(daily_loss_limit=1000.0))
    client = FakeClient(positions=[position(total="-1500")])
    decision = await authorize(monkeypatch, client=client, session=session)
    assert check(decision, "daily_limit").passed is False
    assert session.added == []


# --- the live account's own portfolio limits ------------------------------
#
# Concurrent positions, open exposure and the day's risk allocation used to be
# enforced only by the paper risk engine, which meant real money was bounded by
# a simulation. Every test in this section proves the limit is answered from
# live data: the broker's position book, or the risk the day's live submissions
# were sized from. None of them writes a paper row, and the engine must refuse
# without one.


def allocation(*amounts: str) -> list[tuple[object, object, Decimal]]:
    """One live submission row per amount: (row id, signal id, planned risk)."""
    return [(f"row-{index}", f"signal-{index}", Decimal(amount)) for index, amount in enumerate(amounts)]


async def test_a_day_with_budget_left_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capital 10000 at 4% is 400 for the day; 100 spent and 50 wanted fits."""
    decision = await authorize(monkeypatch, allocated=allocation("100"), candidate_risk=Decimal("50"))
    assert check(decision, "live_allocation").passed is True
    assert check(decision, "live_allocation").data == {
        "allocated": "100",
        "budget": "400.00",
        "candidate": "50",
        "trades": 1,
    }


async def test_the_days_live_risk_budget_being_spent_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = await authorize(monkeypatch, allocated=allocation("200", "150"), candidate_risk=Decimal("100"))
    allocated = check(decision, "live_allocation")
    assert decision.authorized is False
    assert allocated.passed is False
    assert "350 of 400.00 allocated across 2 live trade(s)" in allocated.detail
    assert "this order needs 100" in allocated.detail


async def test_this_orders_own_risk_counts_against_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the last order of the day is always affordable.

    Nothing it is about to spend has been spent yet, so a budget checked only
    against what is already allocated lets a single oversized order through.
    """
    decision = await authorize(monkeypatch, allocated=[], candidate_risk=Decimal("500"))
    assert check(decision, "live_allocation").passed is False
    assert "this order needs 500" in check(decision, "live_allocation").detail


async def test_an_order_that_cannot_say_what_it_risks_does_not_skip_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It loses the reservation for its own risk, not the limit itself."""
    spent = await authorize(monkeypatch, allocated=allocation("450"), candidate_risk=None)
    room = await authorize(monkeypatch, allocated=allocation("100"), candidate_risk=None)
    assert check(spent, "live_allocation").passed is False
    assert check(room, "live_allocation").passed is True


async def test_the_allocation_is_counted_per_trade_not_per_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One trade writes an entry, a stop behind it and an exit.

    The query is distinct by signal, so the reported trade count is the number
    of signals rather than the number of rows -- charging one round trip three
    times would close the day after a third of it.
    """
    decision = await authorize(monkeypatch, allocated=allocation("86", "86", "86"))
    assert check(decision, "live_allocation").data["trades"] == 3
    assert check(decision, "live_allocation").data["allocated"] == "258"


async def test_the_live_position_limit_is_read_from_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """One position open against a limit of one, and this order is a new name."""
    client = FakeClient(positions=[position("TATAPOWER-EQ", token="NSE_EQ|INE245A01021")])
    decision = await authorize(monkeypatch, client=client)
    positions = check(decision, "live_positions")
    assert decision.authorized is False
    assert positions.passed is False
    assert "1 live position(s) are already open and the limit is 1: TATAPOWER-EQ" in positions.detail
    assert positions.data["open_positions"] == 1


async def test_room_under_the_live_position_limit_authorises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(positions=[position("TATAPOWER-EQ", token="NSE_EQ|INE245A01021")])
    decision = await authorize(monkeypatch, client=client, controls=limits(maximum_open_positions=2))
    assert check(decision, "live_positions").passed is True
    assert decision.authorized is True


async def test_a_flat_position_row_does_not_hold_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A squared-off instrument stays in the book all day with net quantity 0."""
    client = FakeClient(positions=[position("TATAPOWER-EQ", netQuantity="0", token="NSE_EQ|INE245A01021")])
    decision = await authorize(monkeypatch, client=client)
    assert check(decision, "live_positions").passed is True
    assert check(decision, "live_positions").data["open_positions"] == 0


async def test_an_order_against_a_token_already_held_is_not_a_new_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding to, reducing or closing a position must not need a free slot."""
    client = FakeClient(positions=[position("IDEA-EQ", token=ORDER.instrument_token)])
    decision = await authorize(monkeypatch, client=client)
    assert check(decision, "live_positions").passed is True


async def test_a_held_position_is_matched_by_its_broker_name_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firstock's book names the symbol; the token comes from the order."""
    client = FakeClient(positions=[position(DESCRIPTION.symbol)])
    decision = await authorize(monkeypatch, client=client)
    assert check(decision, "live_positions").passed is True


async def test_an_unreadable_position_quantity_refuses_rather_than_counting_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(positions=[position("TATAPOWER-EQ", netQuantity="n/a")])
    decision = await authorize(monkeypatch, client=client)
    positions = check(decision, "live_positions")
    assert positions.passed is False
    assert "unreadable quantity for TATAPOWER-EQ" in positions.detail


async def test_a_broker_that_will_not_answer_positions_refuses_the_position_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(positions_raise=RuntimeError("position book unavailable"))
    decision = await authorize(monkeypatch, client=client)
    positions = check(decision, "live_positions")
    assert positions.passed is False
    assert "position book unavailable" in positions.detail


async def test_live_exposure_over_the_ceiling_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 at 500 is 5000 against a ceiling of 10000 * 10% * 5x = 5000... just over."""
    client = FakeClient(positions=[position("TATAPOWER-EQ", lastTradedPrice="501", token="NSE_EQ|INE245A01021")])
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(maximum_open_positions=5, maximum_open_exposure_percent=10.0),
    )
    positions = check(decision, "live_positions")
    assert positions.passed is False
    assert positions.data["exposure"] == "5010"
    assert positions.data["exposure_ceiling"] == "5000.000"
    assert "exceeds the ceiling" in positions.detail


async def test_exposure_uses_the_average_price_when_there_is_no_last_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(positions=[position("TATAPOWER-EQ", netAveragePrice="400", token="NSE_EQ|INE245A01021")])
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(maximum_open_positions=5, maximum_open_exposure_percent=10.0),
    )
    assert check(decision, "live_positions").data["exposure"] == "4000"
    assert check(decision, "live_positions").passed is True


async def test_a_short_position_adds_exposure_rather_than_cancelling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two opposite positions are twice the money at risk, not none of it."""
    client = FakeClient(
        positions=[
            position("A", netQuantity="10", lastTradedPrice="300", token="A-token"),
            position("B", netQuantity="-10", lastTradedPrice="300", token="B-token"),
        ]
    )
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(maximum_open_positions=5, maximum_open_exposure_percent=10.0),
    )
    assert check(decision, "live_positions").data["exposure"] == "6000"


async def test_without_leverage_the_exposure_ceiling_is_the_capital_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(positions=[position("TATAPOWER-EQ", lastTradedPrice="300", token="NSE_EQ|INE245A01021")])
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(
            maximum_open_positions=5,
            maximum_open_exposure_percent=10.0,
            intraday_leverage_enabled=False,
        ),
    )
    positions = check(decision, "live_positions")
    assert positions.data["exposure_ceiling"] == "1000.00"
    assert positions.passed is False


async def test_an_order_reducing_a_position_is_not_refused_by_the_exposure_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ceiling that refuses the order bringing the account back inside it
    would trap the position it was meant to protect against."""
    client = FakeClient(
        positions=[position("IDEA-EQ", netQuantity="100", lastTradedPrice="500", token=ORDER.instrument_token)]
    )
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(maximum_open_positions=5, maximum_open_exposure_percent=10.0),
        side="SELL",
    )
    assert check(decision, "live_positions").passed is True


async def test_adding_to_a_position_over_the_ceiling_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is for the exit, not for the same trade made larger."""
    client = FakeClient(
        positions=[position("IDEA-EQ", netQuantity="100", lastTradedPrice="500", token=ORDER.instrument_token)]
    )
    decision = await authorize(
        monkeypatch,
        client=client,
        controls=limits(maximum_open_positions=5, maximum_open_exposure_percent=10.0),
        side="BUY",
    )
    assert check(decision, "live_positions").passed is False


async def test_saved_controls_that_cannot_be_read_refuse_both_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored value that no longer validates must refuse, not raise.

    Every other check in this engine answers no when it cannot establish its
    fact. These two must not be the pair that throws instead -- and they must
    not fall back to defaults the operator never chose.
    """
    session = FakeSession(reconciliation(), None, limits(), controls_raise=RuntimeError("settings row is corrupt"))
    decision = await authorize(monkeypatch, session=session)
    assert decision.authorized is False
    for key in ("live_allocation", "live_positions"):
        assert check(decision, key).passed is False
        assert "could not be read" in check(decision, key).detail
