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
