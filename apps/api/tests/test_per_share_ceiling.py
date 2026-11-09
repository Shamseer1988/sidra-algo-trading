"""One share, one trade a day -- across every strategy.

On 6 November the account entered JSWSTEEL short twice in the same minute:
``orb-retest-v1`` at 09:38 and ``rs-pullback-v1`` at 09:38, same side, same
quantity, both stopped out at 09:47 for about a full R each. Nothing was
misconfigured. Each strategy was inside its own ``max_trades_per_day`` and its
own ``max_trades_per_side``, and the account was inside ``maximum_daily_trades``
-- because the per-strategy caps cannot see each other, and nothing counted the
share.

Two entries on one share is two trades' worth of risk on one company's news,
which is the one thing position sizing cannot protect against: the stops do not
fail independently.

The counter is shared with the account-wide ceiling, so each clause below is
about the narrowing rather than about counting fills, which
``test_trade_counter.py`` already covers.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.core.config import Settings
from app.db.models import PaperOrder, PaperSignal, ScannerEvaluation
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.strategy_registry import StrategyConfiguration
from app.services.trade_counter import count_filled_entries

SESSION = date(2031, 7, 15)
JSWSTEEL = "NSE_EQ|INEPERSHARE1"
OTHER = "NSE_EQ|INEPERSHARE2"


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **_: object) -> bool:
        self.values[key] = value
        return True

    async def publish(self, _: str, __: str) -> int:
        return 0


def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://sidra:secret@postgres/sidra",
        redis_url="redis://redis:6379/0",
        jwt_secret="a-secure-test-secret-that-is-longer-than-32-characters",
        _env_file=None,
    )


class Decision:
    """The one attribute ``_signal_block_reason`` reads off a decision."""

    def __init__(self, side: str) -> None:
        self.side = side


def strategy(strategy_type: str, name: str) -> StrategyConfiguration:
    # Generous per-strategy caps on purpose: if a clause below blocks, it is the
    # per-share ceiling that blocked it and not one of these.
    return StrategyConfiguration(
        id=f"{strategy_type}-per-share",
        name=name,
        strategy_type=strategy_type,
        max_trades_per_day=20,
        max_trades_per_side=None,
        cooldown_minutes=0,
    )


ORB = strategy("orb-retest-v1", "ORB Retest")
RS = strategy("rs-pullback-v1", "RS Pullback")


def candle(instrument: str, minute: int = 8) -> CompletedCandle:
    opened_at = datetime(2031, 7, 15, 4, minute, tzinfo=UTC)
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
        tick_count=5,
    )


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperOrder).where(PaperOrder.session_date == SESSION))
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date == SESSION))
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == SESSION))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def fill_entry(instrument: str, *, strategy_type: str, side: str, key: str) -> None:
    """One signal on ``instrument`` whose entry received a fill."""
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=key,
            instrument_token=instrument,
            session_date=SESSION,
            candle_opened_at=datetime(2031, 7, 15, 4, 8, tzinfo=UTC),
            strategy_version=f"{strategy_type}@1",
            side=side,
            entry_price=Decimal("100"),
            stop_price=Decimal("101") if side == "SHORT" else Decimal("99"),
            target_price=Decimal("98") if side == "SHORT" else Decimal("102"),
            quantity=11,
            risk_amount=Decimal("125"),
            score=75,
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        session.add(
            PaperOrder(
                paper_signal_id=signal.id,
                client_order_id=f"{key}-entry",
                instrument_token=instrument,
                session_date=SESSION,
                strategy_version=signal.strategy_version,
                side="SELL" if side == "SHORT" else "BUY",
                order_type="MARKET",
                order_role="ENTRY",
                quantity=11,
                filled_quantity=11,
                status="FILLED",
                eligible_after=datetime(2031, 7, 15, 4, 8, tzinfo=UTC),
            )
        )
        await session.commit()


def orchestrator() -> PaperScannerOrchestrator:
    return PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]


# A ceiling high enough that nothing below is blocked by the account-wide one.
CONTROLS = {"maximum_daily_trades": 20, "maximum_daily_trades_per_instrument": 1}


# --- the 6 November case -------------------------------------------------


async def test_a_second_strategy_cannot_enter_a_share_the_first_already_traded() -> None:
    """The defect itself: both were inside their own caps, so nothing stopped them."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    reason = await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("SHORT"), CONTROLS)

    assert reason is not None
    assert reason.startswith("Daily ceiling for this share reached")


async def test_the_reason_says_the_other_strategy_may_be_holding_it() -> None:
    """An operator reading "ceiling reached" on a share this strategy never traded
    would reasonably go looking for a bug in this strategy."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    reason = await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("SHORT"), CONTROLS)

    assert reason is not None
    assert "1 of 1 used" in reason
    assert "the per-strategy caps cannot see each other" in reason


async def test_the_strategy_that_traded_it_is_refused_too() -> None:
    """Not a cross-strategy rule with a hole in it: the share is the subject."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    reason = await orchestrator()._signal_block_reason(candle(JSWSTEEL), ORB, Decision("SHORT"), CONTROLS)

    assert reason is not None
    assert reason.startswith("Daily ceiling for this share reached")


# --- what it must not block ---------------------------------------------


async def test_another_share_is_untouched() -> None:
    """A ceiling that ended the day on the first fill would be the account-wide one."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    assert await orchestrator()._signal_block_reason(candle(OTHER), RS, Decision("SHORT"), CONTROLS) is None


async def test_an_empty_day_blocks_nothing() -> None:
    assert await orchestrator()._signal_block_reason(candle(JSWSTEEL), ORB, Decision("LONG"), CONTROLS) is None


async def test_a_signal_that_never_filled_does_not_spend_the_share() -> None:
    """Counted from fills, like every other ceiling here. A signal whose entry was
    never filled is not a trade the account took on that company."""
    async with SessionLocal() as session:
        session.add(
            PaperSignal(
                signal_key="per-share-unfilled",
                instrument_token=JSWSTEEL,
                session_date=SESSION,
                candle_opened_at=datetime(2031, 7, 15, 4, 8, tzinfo=UTC),
                strategy_version="orb-retest-v1@1",
                side="SHORT",
                entry_price=Decimal("100"),
                stop_price=Decimal("101"),
                target_price=Decimal("98"),
                quantity=11,
                risk_amount=Decimal("125"),
                score=75,
            )
        )
        await session.commit()

    assert await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("SHORT"), CONTROLS) is None


async def test_yesterdays_trade_on_this_share_does_not_block_today() -> None:
    """The narrowing is applied on top of the session-date filter, not instead of it."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    async with SessionLocal() as session:
        today = await count_filled_entries(session, SESSION, instrument_token=JSWSTEEL)
        tomorrow = await count_filled_entries(session, SESSION + timedelta(days=1), instrument_token=JSWSTEEL)

    assert today.total == 1
    assert tomorrow.total == 0


# --- the control itself --------------------------------------------------


async def test_two_allows_a_reversal_on_the_same_share() -> None:
    """One is the default and refuses a same-day reversal as well, which over the
    twenty sessions measured cost nothing: there was one repeat and it was not a
    reversal. Two is how an operator gets reversals back."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    controls = {**CONTROLS, "maximum_daily_trades_per_instrument": 2}
    assert await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("LONG"), controls) is None


async def test_a_settings_row_saved_before_this_control_existed_still_works() -> None:
    """An operator's stored row has no key for a control added after it was saved.
    Indexing it would turn every scan into a KeyError; the default applies instead,
    and the default is the safe direction."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    older = {"maximum_daily_trades": 20}
    reason = await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("SHORT"), older)

    assert reason is not None
    assert reason.startswith("Daily ceiling for this share reached")


async def test_the_account_ceiling_still_reports_first() -> None:
    """Ordered so the reason an operator reads is the one that will keep blocking.
    A day that is out of trades is out of trades on every share."""
    await fill_entry(JSWSTEEL, strategy_type="orb-retest-v1", side="SHORT", key="per-share-orb")

    controls = {"maximum_daily_trades": 1, "maximum_daily_trades_per_instrument": 1}
    reason = await orchestrator()._signal_block_reason(candle(JSWSTEEL), RS, Decision("SHORT"), controls)

    assert reason is not None
    assert reason.startswith("Daily trade ceiling reached")
