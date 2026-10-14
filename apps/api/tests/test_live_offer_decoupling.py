"""A paper refusal withholds a paper order, not a live one.

On 14 October the live account placed one order and stopped. The scanner had
accepted five signals; the paper risk engine reached its own ceiling, returned
early, and the live path -- which never got asked -- simply did not run. The
live account's concurrent-position, exposure and daily-allocation limits were
in practice the paper ledger's.

The live engine now answers each of those from live data, and the one paper
refusal that was a fact about the share rather than about the ledger (the
operator's price band) it answers too. So the scanner offers the live entry
either way. These tests pin that, and pin the one case that still withholds it:
a paper side that raised rather than decided, which has established nothing.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.models import PaperSignal
from app.services import scanner_orchestration as module
from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, SIGNALLED
from app.services.scanner_orchestration import PaperOutcome, PaperScannerOrchestrator
from app.services.strategy_registry import StrategyConfiguration

SESSION = date(2026, 10, 14)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_: object) -> bool:
        self.values[key] = value
        return True

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 0


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://sidra:secret@postgres/sidra",
        "redis_url": "redis://redis:6379/0",
        "jwt_secret": "a-secure-test-secret-that-is-longer-than-32-characters",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def candle() -> CompletedCandle:
    opened_at = datetime(2026, 10, 14, 4, 30, tzinfo=UTC)
    return CompletedCandle(
        instrument_token="NSE_EQ|INEDECOUPLE",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=100,
        tick_count=2,
    )


def signal() -> PaperSignal:
    """The real mapped class, so a field the live path reads cannot go missing."""
    row = PaperSignal(
        signal_key=uuid4().hex,
        instrument_token="NSE_EQ|INEDECOUPLE",
        session_date=SESSION,
        candle_opened_at=datetime(2026, 10, 14, 4, 30, tzinfo=UTC),
        strategy_version="orb-retest-v1@15",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("99.2"),
        target_price=Decimal("101.6"),
        quantity=10,
        risk_amount=Decimal("8"),
        score=80,
    )
    row.id = uuid4()
    return row


class Recorder:
    """The orchestrator with everything but the branch under test stubbed out."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, outcome: PaperOutcome) -> None:
        self.redis = MemoryRedis()
        self.orchestrator = PaperScannerOrchestrator(settings(), self.redis)  # type: ignore[arg-type]
        self.signal = signal()
        self.offered: list[PaperSignal] = []
        self.alerted: list[PaperSignal] = []
        self.reserved: list[PaperSignal] = []
        self.outcome = outcome

        decision = SimpleNamespace(next_state=SIGNALLED, side="LONG", reason="", score_breakdown={})
        monkeypatch.setattr(module.StrategyRegistry, "evaluate", staticmethod(lambda *_a, **_k: decision))

        async def no_block(*_args, **_kwargs):
            return None

        async def record_evaluation(*_args, **_kwargs):
            return None

        async def record(*_args, **_kwargs):
            return self.signal

        async def reserve(sig):  # noqa: ANN001
            self.reserved.append(sig)
            return self.outcome

        async def alert(sig):  # noqa: ANN001
            self.alerted.append(sig)

        async def offer(sig):  # noqa: ANN001
            self.offered.append(sig)

        monkeypatch.setattr(self.orchestrator, "_signal_block_reason", no_block)
        monkeypatch.setattr(self.orchestrator, "_record_evaluation", record_evaluation)
        monkeypatch.setattr(self.orchestrator, "_record", record)
        monkeypatch.setattr(self.orchestrator, "_reserve_paper", reserve)
        monkeypatch.setattr(self.orchestrator, "_alert", alert)
        monkeypatch.setattr(self.orchestrator, "_offer_live_entry", offer)

    async def run(self) -> None:
        await self.orchestrator._evaluate_strategy(
            candle(),
            {},
            {},
            {"maximum_daily_trades": 4},
            StrategyConfiguration(id="orb-retest-v1", name="ORB Retest"),
        )


# --- the decoupling -------------------------------------------------------


async def test_a_paper_refusal_still_offers_the_live_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this change exists for.

    Reason text taken from the paper engine verbatim: these are the three the
    live engine now answers for itself from live data.
    """
    for reason in (
        "Daily paper-risk allocation limit reached",
        "Maximum concurrent paper positions reached",
        "Maximum paper exposure limit reached",
    ):
        state = Recorder(monkeypatch, PaperOutcome(queued=False, blocks_live=False, reason=reason))
        await state.run()
        assert [row.id for row in state.offered] == [state.signal.id], reason


async def test_a_paper_refusal_still_withholds_the_paper_order_and_its_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paper's own behaviour is unchanged: no alert, no last-signal, no publish.

    The refusal is still recorded against the signal by _reserve_paper, which
    is the part an operator reads; what must not happen is the paper journal
    gaining an entry its own engine refused.
    """
    state = Recorder(monkeypatch, PaperOutcome(False, False, "Maximum paper exposure limit reached"))
    await state.run()
    assert state.alerted == []
    assert state.redis.published == []
    assert not [key for key in state.redis.values if key.startswith("scanner:last_signal:")]


async def test_a_paper_side_that_raised_withholds_the_live_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is the engine working; a raise is the engine not working.

    Nothing has been established about this signal's paper side, and nothing is
    not a reason to send real money on the strength of the gates that did
    answer.
    """
    state = Recorder(monkeypatch, PaperOutcome(False, True, "Paper risk reservation unavailable"))
    await state.run()
    assert state.offered == []
    assert state.alerted == []


async def test_an_accepted_signal_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alert, last-signal, live offer and publish, in that order as before."""
    state = Recorder(monkeypatch, PaperOutcome(True, False, "Paper risk reserved"))
    await state.run()
    assert [row.id for row in state.alerted] == [state.signal.id]
    assert [row.id for row in state.offered] == [state.signal.id]
    assert len(state.redis.published) == 1
    assert f"scanner:last_signal:{candle().instrument_token}" in state.redis.values


async def test_a_signal_the_strategy_never_produced_reaches_neither_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gates before the signal -- cooldown, universe, the daily trade cap --
    still stop both paths, because they stop the signal itself."""
    state = Recorder(monkeypatch, PaperOutcome(True, False, "Paper risk reserved"))

    async def blocked(*_args, **_kwargs):
        return "Daily trade ceiling reached"

    monkeypatch.setattr(state.orchestrator, "_signal_block_reason", blocked)
    monkeypatch.setattr(
        module.StrategyRegistry,
        "evaluate",
        staticmethod(lambda *_a, **_k: SimpleNamespace(next_state=AWAITING, side=None, reason="", score_breakdown={})),
    )
    await state.run()
    assert state.reserved == []
    assert state.offered == []


# --- what _reserve_paper itself reports -----------------------------------


class FakeEngine:
    def __init__(self, decision=None, raises: Exception | None = None) -> None:  # noqa: ANN001
        self._decision = decision
        self._raises = raises

    async def reserve_signal(self, _signal):  # noqa: ANN001
        if self._raises:
            raise self._raises
        return self._decision


class FakeQueue:
    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.queued: list[object] = []

    async def queue_signal(self, sig):  # noqa: ANN001
        if self._raises:
            raise self._raises
        self.queued.append(sig)


async def reserve(monkeypatch: pytest.MonkeyPatch, *, engine: FakeEngine, queue: FakeQueue | None = None):
    queue = queue or FakeQueue()
    marked: list[tuple[object, str]] = []
    orchestrator = PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]

    async def mark(sig, reason):  # noqa: ANN001
        marked.append((sig.id, reason))

    monkeypatch.setattr(module, "PaperRiskEngine", lambda: engine)
    monkeypatch.setattr(module, "PaperOrderManager", lambda: queue)
    monkeypatch.setattr(orchestrator, "_mark_signal_risk_block", mark)
    outcome = await orchestrator._reserve_paper(signal())
    return outcome, marked, queue


async def test_a_reserved_signal_is_queued_and_blocks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = SimpleNamespace(allowed=True, reason="Paper risk reserved")
    outcome, marked, queue = await reserve(monkeypatch, engine=FakeEngine(decision))
    assert outcome == PaperOutcome(True, False, "Paper risk reserved")
    assert len(queue.queued) == 1
    assert marked == []


async def test_a_refused_signal_is_marked_and_queues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = SimpleNamespace(allowed=False, reason="Maximum concurrent paper positions reached")
    outcome, marked, queue = await reserve(monkeypatch, engine=FakeEngine(decision))
    assert outcome.queued is False
    assert outcome.blocks_live is False
    assert outcome.reason == "Maximum concurrent paper positions reached"
    assert queue.queued == []
    assert [reason for _, reason in marked] == ["Maximum concurrent paper positions reached"]


async def test_a_reservation_that_raised_blocks_live(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, marked, queue = await reserve(monkeypatch, engine=FakeEngine(raises=RuntimeError("advisory lock lost")))
    assert outcome == PaperOutcome(False, True, "Paper risk reservation unavailable")
    assert queue.queued == []
    assert [reason for _, reason in marked] == ["Paper risk reservation unavailable"]


async def test_a_queue_failure_after_a_live_reservation_blocks_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reservation is ACTIVE and the paper order is not. The paper side
    broke rather than decided, so the live order is withheld with it."""
    decision = SimpleNamespace(allowed=True, reason="Paper risk reserved")
    outcome, marked, _ = await reserve(
        monkeypatch, engine=FakeEngine(decision), queue=FakeQueue(raises=RuntimeError("queue down"))
    )
    assert outcome == PaperOutcome(False, True, "Paper order could not be queued")
    assert [reason for _, reason in marked] == ["Paper order could not be queued"]


def test_the_outcome_type_does_not_make_blocking_the_opposite_of_queued() -> None:
    """Written as a test because the two fields read like negations of each other
    and are not: the whole change is that a refusal sets neither."""
    refused = PaperOutcome(queued=False, blocks_live=False, reason="Maximum paper exposure limit reached")
    assert refused.queued is False and refused.blocks_live is False
    assert replace(refused, blocks_live=True).blocks_live is True
