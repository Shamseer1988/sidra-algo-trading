from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.db.models import InstrumentMasterRefresh
from app.db.session import SessionLocal, engine
from app.services.trading_symbols import resolve_script_name, resolve_script_names


def test_resolve_script_name_static_parse_and_fallback() -> None:
    assert resolve_script_name("NSE_EQ|INE002A01018") == "RELIANCE"  # static table
    assert resolve_script_name("NSE_INDEX|Nifty 50") == "NIFTY 50"
    assert resolve_script_name("NSE_EQ|TATASTEEL") == "TATASTEEL"  # parseable suffix
    assert resolve_script_name("NSE_EQ|INE999Z01011") == "NSE_EQ|INE999Z01011"  # unknown ISIN -> raw


async def test_resolve_script_names_uses_the_persisted_instrument_master() -> None:
    await engine.dispose()
    marker = "test-instrument-master"
    record = InstrumentMasterRefresh(
        provider="UPSTOX",
        source_url=marker,
        payload_sha256="0" * 64,
        instrument_count=1,
        configured_keys={
            "NSE_EQ|INE999Z01011": {"instrument_key": "NSE_EQ|INE999Z01011", "trading_symbol": "ZEELEARN"}
        },
        missing_keys=[],
    )
    try:
        async with SessionLocal() as session:
            session.add(record)
            await session.commit()
            names = await resolve_script_names(session, ["NSE_EQ|INE999Z01011", "NSE_EQ|INE002A01018", "NSE_EQ|INFY"])
        assert names["NSE_EQ|INE999Z01011"] == "ZEELEARN"  # from the instrument master
        assert names["NSE_EQ|INE002A01018"] == "RELIANCE"  # still from the static table
        assert names["NSE_EQ|INFY"] == "INFY"  # parseable suffix
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(InstrumentMasterRefresh).where(InstrumentMasterRefresh.source_url == marker))
            await session.commit()
        await engine.dispose()


# --- the tick size, which was downloaded and thrown away ---------------------


async def _store_master(entries: dict):
    from app.db.models import InstrumentMasterRefresh
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        session.add(
            InstrumentMasterRefresh(
                provider="UPSTOX",
                source_url="test",
                payload_sha256="x",
                instrument_count=len(entries),
                configured_keys=entries,
                missing_keys=[],
            )
        )
        await session.commit()


@pytest.fixture
async def clean_master():
    from sqlalchemy import delete

    from app.db.models import InstrumentMasterRefresh
    from app.db.session import SessionLocal

    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(InstrumentMasterRefresh).where(InstrumentMasterRefresh.source_url == "test"))
            await session.commit()

    await wipe()
    yield
    await wipe()


async def test_the_tick_size_is_read_in_rupees_not_paise(clean_master) -> None:
    """Upstox reports it in paise: 10.0 means ₹0.10. Read as rupees it would be
    a ten-rupee grid, which refuses every order instead of some of them."""
    from app.db.session import SessionLocal
    from app.services.trading_symbols import instrument_tick_size

    await _store_master(
        {
            "NSE_EQ|INE982J01020": {"trading_symbol": "PAYTM", "tick_size": 10.0},
            "NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0},
            "NSE_EQ|INE123456789": {"trading_symbol": "PENNY", "tick_size": 1.0},
        }
    )
    async with SessionLocal() as session:
        assert await instrument_tick_size(session, "NSE_EQ|INE982J01020") == Decimal("0.1000")
        assert await instrument_tick_size(session, "NSE_EQ|INE335Y01020") == Decimal("0.0500")
        assert await instrument_tick_size(session, "NSE_EQ|INE123456789") == Decimal("0.0100")


async def test_an_instrument_we_have_no_grid_for_is_none_not_a_guess(clean_master) -> None:
    """None rather than a default, so the caller decides what unknown means.
    Guessing here would reproduce the defect this exists to end."""
    from app.db.session import SessionLocal
    from app.services.trading_symbols import instrument_tick_size

    await _store_master({"NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0}})
    async with SessionLocal() as session:
        assert await instrument_tick_size(session, "NSE_EQ|INE000000000") is None


@pytest.mark.parametrize("value", [None, 0, -5, "", "abc", {}])
async def test_an_unusable_tick_is_none(clean_master, value) -> None:
    from app.db.session import SessionLocal
    from app.services.trading_symbols import instrument_tick_size

    await _store_master({"NSE_EQ|INE335Y01020": {"tick_size": value}})
    async with SessionLocal() as session:
        assert await instrument_tick_size(session, "NSE_EQ|INE335Y01020") is None
