"""One bet per group of instruments that move together.

On 9 November the account opened four positions at 09:37:01, and three of them
-- SBIN, PNB and BANKBARODA -- were PSU banks. Every ceiling passed: the
account was inside 3 trades, each share was entered once, and each strategy was
inside its own cap. None of them could tell that three of the four were the
same bet, because none of them knows what moves with what.

Those three stops do not fail independently. One move in the banking pack takes
all of them, so 375 of planned risk was in practice one position of 340 and a
second of 116.

The map is the fragile part, so most of what is tested here is what happens
when it is wrong: a stale map must cost protection and must never cost a trade.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core.config import Settings
from app.db.models import LiveOrderSubmission, PaperOrder, PaperSignal
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.scanner_orchestration import PaperScannerOrchestrator
from app.services.sectors import SECTORS, members, normalise, sector_of, unmapped
from app.services.strategy_registry import StrategyConfiguration
from app.services.trade_counter import filled_entry_instruments, session_bounds_utc

SESSION = date(2031, 5, 19)
# The three that actually did it, plus one from another group.
SBIN = "NSE_EQ|INESECTOR0SBIN"
PNB = "NSE_EQ|INESECTOR00PNB"
BARODA = "NSE_EQ|INESECTOR0BOB0"
VEDL = "NSE_EQ|INESECTOR0VEDL"
NAMES = {SBIN: "SBIN", PNB: "PNB", BARODA: "BANKBARODA", VEDL: "VEDL"}


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, nx: bool = False, **_: object) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def eval(self, _script: str, _n: int, key: str, token: str) -> int:
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


class Decision:
    def __init__(self, side: str = "LONG") -> None:
        self.side = side


def candle(instrument: str) -> CompletedCandle:
    opened = datetime(2031, 5, 19, 4, 8, tzinfo=UTC)
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
        tick_count=5,
    )


def strategy() -> StrategyConfiguration:
    return StrategyConfiguration(
        id="sector-test",
        name="RS Pullback",
        strategy_type="rs-pullback-v1",
        max_trades_per_day=20,
        cooldown_minutes=0,
    )


async def clean() -> None:
    start, end = session_bounds_utc(SESSION)
    async with SessionLocal() as session:
        await session.execute(delete(PaperOrder).where(PaperOrder.session_date == SESSION))
        await session.execute(delete(PaperSignal).where(PaperSignal.session_date == SESSION))
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def filled(instrument: str) -> None:
    """One trade on this instrument, written the way the live path writes it.

    The signal first and the submission carrying its id, because that is what
    ``_offer_live_entry`` does and because the per-share ceiling narrows through
    ``paper_signals``: a submission with no signal behind it is invisible to
    that count, so a test that skipped the signal would be testing a shape the
    scanner never produces.
    """
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"sector-{uuid4().hex}",
            instrument_token=instrument,
            session_date=SESSION,
            candle_opened_at=datetime(2031, 5, 19, 4, 7, tzinfo=UTC),
            strategy_version="rs-pullback-v1@8",
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
                trading_symbol=instrument,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("100"),
                planned_risk=Decimal("125"),
                status="ACCEPTED",
                created_at=datetime(2031, 5, 19, 4, 7, 1, tzinfo=UTC),
                request_snapshot={"canonical": {"instrumentToken": instrument}},
            )
        )
        await session.commit()


def build(monkeypatch: pytest.MonkeyPatch, names: dict[str, str] | None = None) -> PaperScannerOrchestrator:
    """The orchestrator with the instrument-master lookup answered from NAMES.

    Patched rather than seeded because the real resolver reads a refresh row
    this test has no business writing, and the mapping it returns is the only
    part of it the gate uses.
    """
    table = NAMES if names is None else names

    async def resolve(_session, tokens):  # noqa: ANN001
        return {token: table.get(token, token) for token in tokens}

    monkeypatch.setattr("app.services.scanner_orchestration.resolve_script_names", resolve)
    return PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]


CONTROLS = {
    "maximum_daily_trades": 20,
    "maximum_daily_trades_per_instrument": 1,
    "maximum_daily_trades_per_sector": 1,
}


# --- the 9 November case -------------------------------------------------


async def test_a_second_bank_is_refused_after_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    await filled(SBIN)
    orchestrator = build(monkeypatch)

    reason = await orchestrator._signal_block_reason(candle(PNB), strategy(), Decision(), CONTROLS)

    assert reason is not None
    assert "BANK group reached" in reason
    assert "SBIN" in reason


async def test_the_third_bank_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    await filled(SBIN)
    await filled(PNB)
    orchestrator = build(monkeypatch)

    reason = await orchestrator._signal_block_reason(candle(BARODA), strategy(), Decision(), CONTROLS)

    assert reason is not None
    assert "SBIN" in reason and "PNB" in reason


async def test_the_reason_says_why_rather_than_only_that(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator reading "ceiling reached" on a share never traded today would
    go looking for a bug."""
    await filled(SBIN)
    orchestrator = build(monkeypatch)

    reason = await orchestrator._signal_block_reason(candle(PNB), strategy(), Decision(), CONTROLS)

    assert "move together" in reason
    assert "same bet twice" in reason


# --- what it must not refuse ---------------------------------------------


async def test_another_group_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """VEDL is a metal. A ceiling that ended the day on the first fill would be
    the account-wide one."""
    await filled(SBIN)
    orchestrator = build(monkeypatch)

    assert await orchestrator._signal_block_reason(candle(VEDL), strategy(), Decision(), CONTROLS) is None


async def test_an_empty_day_refuses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = build(monkeypatch)
    assert await orchestrator._signal_block_reason(candle(SBIN), strategy(), Decision(), CONTROLS) is None


async def test_two_allows_a_second_name_from_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    await filled(SBIN)
    orchestrator = build(monkeypatch)
    controls = {**CONTROLS, "maximum_daily_trades_per_sector": 2}

    assert await orchestrator._signal_block_reason(candle(PNB), strategy(), Decision(), controls) is None


async def test_a_settings_row_saved_before_this_control_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    await filled(SBIN)
    orchestrator = build(monkeypatch)

    reason = await orchestrator._signal_block_reason(
        candle(PNB), strategy(), Decision(), {"maximum_daily_trades": 20}
    )

    assert reason is not None
    assert "BANK group reached" in reason


# --- a wrong map must cost protection, never a trade ---------------------


async def test_an_unmapped_name_is_its_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two names the map does not know must not be pooled together: that would
    refuse a real signal because of a stale file."""
    await filled(SBIN)
    orchestrator = build(monkeypatch, names={SBIN: "NEWCO1", PNB: "NEWCO2"})

    assert await orchestrator._signal_block_reason(candle(PNB), strategy(), Decision(), CONTROLS) is None


async def test_a_name_lookup_failure_does_not_refuse_the_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling stops protecting and never starts refusing. Blocking a real
    signal because the instrument master could not be read would be worse than
    the concentration this gate exists to prevent."""
    await filled(SBIN)

    async def broken(_session, _tokens):  # noqa: ANN001
        raise OSError("instrument master unreadable")

    monkeypatch.setattr("app.services.scanner_orchestration.resolve_script_names", broken)
    orchestrator = PaperScannerOrchestrator(settings(), MemoryRedis())  # type: ignore[arg-type]

    assert await orchestrator._signal_block_reason(candle(PNB), strategy(), Decision(), CONTROLS) is None


async def test_the_same_share_twice_is_the_share_ceiling_not_this_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordered so the reason an operator reads is the narrower, truer one."""
    await filled(SBIN)
    orchestrator = build(monkeypatch)

    reason = await orchestrator._signal_block_reason(candle(SBIN), strategy(), Decision(), CONTROLS)

    assert reason is not None
    assert reason.startswith("Daily ceiling for this share reached")


# --- what the day has taken ----------------------------------------------


async def test_the_instruments_of_todays_entries_are_listed_once() -> None:
    await filled(SBIN)
    await filled(PNB)
    async with SessionLocal() as session:
        taken = await filled_entry_instruments(session, SESSION)
    assert sorted(taken) == sorted((SBIN, PNB))


async def test_a_protective_stop_is_not_an_entry() -> None:
    """planned_risk is null on a stop and on an exit, which is what separates
    them from the order that opened the position."""
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:16]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=SBIN,
                product="I",
                price_type="SL-M",
                transaction_type="SELL",
                quantity=10,
                price=Decimal("0"),
                planned_risk=None,
                status="ACCEPTED",
                created_at=datetime(2031, 5, 19, 4, 7, 2, tzinfo=UTC),
                request_snapshot={"canonical": {"instrumentToken": SBIN}},
            )
        )
        await session.commit()
    async with SessionLocal() as session:
        assert await filled_entry_instruments(session, SESSION) == ()


async def test_yesterdays_entries_are_not_todays() -> None:
    await filled(SBIN)
    async with SessionLocal() as session:
        assert await filled_entry_instruments(session, SESSION + timedelta(days=1)) == ()


# --- the map itself -------------------------------------------------------


def test_the_three_that_did_it_are_one_group():
    assert sector_of("SBIN") == sector_of("PNB") == sector_of("BANKBARODA") == "BANK"


def test_a_broker_suffix_does_not_hide_a_name():
    """Firstock reports BANKBARODA-EQ and the map is keyed by the bare name."""
    assert sector_of("BANKBARODA-EQ") == "BANK"
    assert normalise("  sbin-eq ") == "SBIN"


def test_the_adani_names_are_one_group_though_their_industries_differ():
    """Group news moves both, which is what this map is for."""
    assert sector_of("ADANIENT") == sector_of("ADANIPORTS") == "ADANI"


def test_the_psu_financiers_are_one_group():
    assert members("PSU_FIN") == ["IRFC", "PFC", "RECLTD"]


def test_an_unknown_name_gets_a_group_of_its_own():
    assert sector_of("NEWCO") == "UNMAPPED:NEWCO"
    assert sector_of("NEWCO") != sector_of("OTHERCO")


def test_unmapped_reports_what_a_watchlist_is_missing():
    assert unmapped(["SBIN", "NEWCO", "sbin-eq", "OTHERCO"]) == ["NEWCO", "OTHERCO"]


def test_every_group_name_is_plain_and_every_entry_is_upper_case():
    """The map is maintained by hand beside the watchlist; a lower-case key
    would silently never match."""
    for name, group in SECTORS.items():
        assert name == name.upper().strip(), name
        assert group == group.upper().strip(), group
        assert not group.startswith("UNMAPPED"), group
