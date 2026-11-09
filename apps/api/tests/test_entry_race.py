"""Two signals in the same second cannot both pass a ceiling of one.

This is 9 November, reproduced. Four entries left for the broker at 09:37:01 --
SBIN, PNB, BANKBARODA and VEDL -- and all three daily gates passed all four:

    maximum_daily_trades        3      -> 4 taken
    maximum_daily_risk_percent  1.5%   -> 500 committed against 375
    maximum_open_positions      2      -> four held together until 12:10

Nothing was misconfigured. Each entry was checked while the other three were
still in flight, and every gate read state that is only written once an order
has been sent. Two things call the scanner at once -- the feed task and the
supervisor's flush_expired -- so "checked, then sent" had a gap in it wide
enough to send another order through.

The fix has two halves and this file tests them together, because either on its
own leaves a window:

  * an in-flight submission counts toward the ceiling, so once the row exists
    every gate can see it;
  * a lock spans the check and the insert, so the row always exists before the
    next signal is checked.

The last test removes the lock and watches both entries go through, which is
what the gap looked like and the only way to know these tests have teeth.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.core.config import Settings
from app.db.models import LiveOrderSubmission, PaperSignal
from app.db.session import SessionLocal
from app.services import scanner_orchestration as module
from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import SIGNALLED
from app.services.scanner_orchestration import PaperOutcome, PaperScannerOrchestrator
from app.services.strategy_registry import StrategyConfiguration
from app.services.trade_counter import session_bounds_utc

SESSION = date(2031, 9, 18)
SBIN = "NSE_EQ|INERACE0SBIN"
PNB = "NSE_EQ|INERACE00PNB"
# Each chain's stubbed round trip. Long enough that two chains started together
# genuinely overlap, short enough that the suite does not notice.
ROUND_TRIP = 0.05


class MemoryRedis:
    """Honours nx so the lock under test can actually fail to be taken."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, nx: bool = False, **_: object) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, token: str) -> int:
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0

    async def publish(self, _channel: str, _payload: str) -> int:
        return 0


def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://sidra:secret@postgres/sidra",
        redis_url="redis://redis:6379/0",
        jwt_secret="a-secure-test-secret-that-is-longer-than-32-characters",
        _env_file=None,
    )


def candle(instrument: str) -> CompletedCandle:
    opened_at = datetime(2031, 9, 18, 4, 7, tzinfo=UTC)
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=1000,
        tick_count=5,
    )


async def clean() -> None:
    start, end = session_bounds_utc(SESSION)
    async with SessionLocal() as session:
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date == SESSION))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def live_entries_today() -> list[LiveOrderSubmission]:
    start, end = session_bounds_utc(SESSION)
    async with SessionLocal() as session:
        rows = await session.scalars(
            select(LiveOrderSubmission)
            .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
            .order_by(LiveOrderSubmission.created_at)
        )
        return list(rows.all())


class Chain:
    """One instrument's trip through the scanner, with the real quota gate.

    ``_record`` and ``_offer_live_entry`` are the only stubs, and both do what
    the real ones do in the order the real ones do it: the signal row first,
    then a submission row committed as PREPARED *before* the request leaves,
    then the broker's answer. The sleep in the middle is the round trip, which
    is the window the gates used to be blind through.
    """

    def __init__(self, orchestrator: PaperScannerOrchestrator, instrument: str) -> None:
        self.orchestrator = orchestrator
        self.instrument = instrument
        self.blocked: str | None = None

    async def record(self, candle_in, decision, *_args, **_kwargs):  # noqa: ANN001
        async with SessionLocal() as session:
            row = PaperSignal(
                signal_key=f"race-{uuid4().hex}",
                instrument_token=self.instrument,
                session_date=SESSION,
                candle_opened_at=candle_in.opened_at,
                strategy_version="orb-retest-v1@1",
                side="LONG",
                entry_price=Decimal("100"),
                stop_price=Decimal("99"),
                target_price=Decimal("102"),
                quantity=10,
                risk_amount=Decimal("125"),
                score=75,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            session.expunge(row)
        return row

    async def offer_live_entry(self, signal: PaperSignal) -> None:
        async with SessionLocal() as session:
            submission = LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:16]}",
                paper_signal_id=signal.id,
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=self.instrument,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("100"),
                planned_risk=Decimal("125"),
                status="PREPARED",
                created_at=datetime(2031, 9, 18, 4, 7, 1, tzinfo=UTC),
            )
            session.add(submission)
            # Committed before the request leaves, exactly as live_orders does:
            # an order at the exchange this system has no record of is the worse
            # failure, and it is also what makes the ceiling able to see it.
            await session.commit()
            submission_id = submission.id
        await asyncio.sleep(ROUND_TRIP)
        async with SessionLocal() as session:
            row = await session.get(LiveOrderSubmission, submission_id)
            row.status = "ACCEPTED"
            await session.commit()

    async def run(self, controls: dict) -> None:
        strategy = StrategyConfiguration(
            id=f"race-{self.instrument[-4:]}",
            name="ORB Retest",
            strategy_type="orb-retest-v1",
            max_trades_per_day=20,
            cooldown_minutes=0,
        )
        await self.orchestrator._evaluate_strategy(candle(self.instrument), {}, {}, controls, strategy)


def build(monkeypatch: pytest.MonkeyPatch) -> tuple[PaperScannerOrchestrator, dict[str, Chain]]:
    orchestrator = PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]
    decision = SimpleNamespace(next_state=SIGNALLED, side="LONG", reason="", score_breakdown={})
    monkeypatch.setattr(module.StrategyRegistry, "evaluate", staticmethod(lambda *_a, **_k: decision))

    reasons: dict[str, str | None] = {}
    real_block_reason = orchestrator._signal_block_reason

    async def record_evaluation(_candle, _indicators, _strategy, taken, _snapshot):  # noqa: ANN001
        # The evaluation row is where a blocked signal's reason is kept; this
        # test reads it from here instead of the database.
        reasons[_candle.instrument_token] = getattr(taken, "reason", None) or None
        return None

    async def reserve(_signal):  # noqa: ANN001
        return PaperOutcome(queued=False, blocks_live=False, reason="")

    async def alert(_signal):  # noqa: ANN001
        return None

    monkeypatch.setattr(orchestrator, "_record_evaluation", record_evaluation)
    monkeypatch.setattr(orchestrator, "_reserve_paper", reserve)
    monkeypatch.setattr(orchestrator, "_alert", alert)
    monkeypatch.setattr(orchestrator, "_signal_block_reason", real_block_reason)

    chains = {SBIN: Chain(orchestrator, SBIN), PNB: Chain(orchestrator, PNB)}

    async def record(candle_in, decision_in, *args, **kwargs):  # noqa: ANN001
        return await chains[candle_in.instrument_token].record(candle_in, decision_in, *args, **kwargs)

    async def offer(signal):  # noqa: ANN001
        return await chains[signal.instrument_token].offer_live_entry(signal)

    monkeypatch.setattr(orchestrator, "_record", record)
    monkeypatch.setattr(orchestrator, "_offer_live_entry", offer)
    orchestrator._test_reasons = reasons  # type: ignore[attr-defined]
    return orchestrator, chains


# --- the defect, fixed ----------------------------------------------------

CEILING_OF_ONE = {"maximum_daily_trades": 1, "maximum_daily_trades_per_instrument": 1}


async def test_two_signals_in_the_same_second_take_one_trade_not_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different shares, so only the account-wide ceiling can refuse the second."""
    orchestrator, chains = build(monkeypatch)

    await asyncio.gather(*(chain.run(CEILING_OF_ONE) for chain in chains.values()))

    entries = await live_entries_today()
    assert len(entries) == 1, [f"{row.trading_symbol} {row.status}" for row in entries]


async def test_the_one_that_was_refused_says_the_ceiling_refused_it(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator, chains = build(monkeypatch)

    await asyncio.gather(*(chain.run(CEILING_OF_ONE) for chain in chains.values()))

    refusals = [text for text in orchestrator._test_reasons.values() if text]
    assert len(refusals) == 1
    assert "ceiling" in refusals[0].lower() or "being placed" in refusals[0].lower()


async def test_a_ceiling_of_two_still_takes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock serialises; it does not refuse. A fix that cost every second
    signal would be a worse trade than the bug."""
    orchestrator, chains = build(monkeypatch)

    await asyncio.gather(*(chain.run({"maximum_daily_trades": 2, "maximum_daily_trades_per_instrument": 1})
                           for chain in chains.values()))

    assert len(await live_entries_today()) == 2


async def test_an_in_flight_entry_alone_is_enough_to_refuse_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half the fix on its own: a PREPARED row that never reaches the broker
    still holds its place, because it may be at the exchange."""
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"race-{uuid4().hex}",
            instrument_token=SBIN,
            session_date=SESSION,
            candle_opened_at=datetime(2031, 9, 18, 4, 7, tzinfo=UTC),
            strategy_version="orb-retest-v1@1",
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("99"),
            target_price=Decimal("102"),
            quantity=10,
            risk_amount=Decimal("125"),
            score=75,
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:16]}",
                paper_signal_id=signal.id,
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=SBIN,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("100"),
                planned_risk=Decimal("125"),
                status="PREPARED",
                created_at=datetime(2031, 9, 18, 4, 7, 1, tzinfo=UTC),
            )
        )
        await session.commit()

    orchestrator, chains = build(monkeypatch)
    await chains[PNB].run(CEILING_OF_ONE)

    entries = await live_entries_today()
    assert len(entries) == 1
    assert entries[0].trading_symbol == SBIN


# --- proof the tests above have teeth ------------------------------------


async def test_without_the_lock_both_entries_go_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """What 9 November was. Removing only the lock is enough to bring it back:
    two chains check the ceiling before either has inserted its row, and a
    ceiling of one takes two trades.

    Kept as a test rather than a comment because the first test in this file
    passes for two different reasons -- the lock, or the in-flight count -- and
    without this one a regression in the lock would pass silently.
    """

    @asynccontextmanager
    async def no_lock(*_args, **_kwargs):
        yield SimpleNamespace(held=True, block_reason=None)

    monkeypatch.setattr(module, "entry_gate", no_lock)
    orchestrator, chains = build(monkeypatch)

    await asyncio.gather(*(chain.run(CEILING_OF_ONE) for chain in chains.values()))

    assert len(await live_entries_today()) == 2
