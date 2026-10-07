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

# What every stored entry must carry. A record written before one of these
# existed is stale however recently it was fetched, and asking for it again
# costs one download the system already makes daily.
REQUIRED_FIELDS = ("trading_symbol", "tick_size")


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
            # tick_size is the field this master was being downloaded and
            # discarded for. NSE's cash tick is not one number: 7,973 equities
            # trade on ₹0.01, 1,349 on ₹0.05 and 461 on ₹0.10 or coarser. An
            # order priced on the wrong grid is refused outright, which is how
            # a PAYTM entry at ₹1,749.95 was rejected on 7 October while an
            # IRCTC entry at ₹449.75 filled: both are multiples of ₹0.05, and
            # only one of those shares trades on a ₹0.05 grid.
            for field in ("instrument_key", "trading_symbol", "segment", "instrument_type", "isin", "tick_size")
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
    if _missing_fields(latest):
        # Stale in shape rather than in age. The master is re-read for every
        # subscribed instrument, so widening what is stored leaves the existing
        # record correct-looking and incomplete -- and on 7 October that meant a
        # deployment carrying the per-instrument tick fix still priced every
        # order on the fallback grid, because the last fetch was recent enough
        # not to be due and had no tick sizes in it. Age alone could not see it.
        return True
    return (datetime.now(UTC) - latest.fetched_at).total_seconds() >= settings.upstox_instrument_refresh_hours * 3600


def _missing_fields(record: InstrumentMasterRefresh) -> bool:
    """Does this stored master predate a field the system now needs?"""
    entries = [value for value in (record.configured_keys or {}).values() if isinstance(value, dict)]
    if not entries:
        return False
    return any(field not in entry for entry in entries for field in REQUIRED_FIELDS)
