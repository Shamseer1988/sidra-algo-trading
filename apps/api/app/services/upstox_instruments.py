"""Daily validation refresh of Upstox's published NSE instrument master."""

import gzip
import hashlib
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import desc, select

from app.core.config import Settings
from app.db.models import InstrumentMasterRefresh
from app.db.session import SessionLocal
from app.services.upstox_market_data import configured_subscriptions

NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


class InstrumentRefreshError(RuntimeError):
    pass


async def refresh_upstox_instruments(settings: Settings) -> InstrumentMasterRefresh:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.get(NSE_INSTRUMENTS_URL)
        response.raise_for_status()
        payload = response.content
        instruments = json.loads(gzip.decompress(payload))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise InstrumentRefreshError("Could not download or parse the Upstox NSE instrument master") from exc
    if not isinstance(instruments, list):
        raise InstrumentRefreshError("Upstox instrument master has an unexpected format")
    by_key = {
        item.get("instrument_key"): item
        for item in instruments
        if isinstance(item, dict) and isinstance(item.get("instrument_key"), str)
    }
    configured = set(configured_subscriptions(settings)) | {settings.upstox_nifty_benchmark_key}
    selected = {
        key: {
            field: by_key[key].get(field)
            for field in ("instrument_key", "trading_symbol", "segment", "instrument_type", "isin")
        }
        for key in configured
        if key in by_key
    }
    record = InstrumentMasterRefresh(
        provider="UPSTOX",
        source_url=NSE_INSTRUMENTS_URL,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        instrument_count=len(instruments),
        configured_keys=selected,
        missing_keys=sorted(configured - set(selected)),
    )
    async with SessionLocal() as session:
        session.add(record)
        await session.commit()
        await session.refresh(record)
    return record


async def refresh_is_due(settings: Settings) -> bool:
    async with SessionLocal() as session:
        latest = await session.scalar(
            select(InstrumentMasterRefresh)
            .where(InstrumentMasterRefresh.provider == "UPSTOX")
            .order_by(desc(InstrumentMasterRefresh.fetched_at))
            .limit(1)
        )
    if latest is None:
        return True
    return (datetime.now(UTC) - latest.fetched_at).total_seconds() >= settings.upstox_instrument_refresh_hours * 3600
