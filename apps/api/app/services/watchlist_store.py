"""The intraday watchlist and benchmark, kept from the screen instead of ``.env``.

Strategies > Subscriptions edits two things that used to live only in the
server's environment file:

* ``UPSTOX_SUBSCRIPTIONS`` -- the shares the live feed streams and the intraday
  scanner can signal on (so the live strategy can trade them with real money);
* ``UPSTOX_NIFTY_BENCHMARK_KEY`` -- the index relative strength and the market
  regime are measured against.

Saved here they are layered over the environment, the way the connection
credentials are: with nothing saved, every reader sees exactly what ``.env``
says, so a deployment that never opens this screen behaves as before.

**When a change takes effect.** The scanner resolves the watchlist once a
session, like the indicator periods, and keeps re-reading it until 9:00 AM IST.
A change saved before 9:00 applies that morning; one saved during the session
waits for the next trading day. One day's scan is never two different lists.

**Names are confirmed, not guessed.** A share is added by picking it from a
search that shows NSE's official company name (from the NIFTY 200 constituents
file, ``app/data/nifty200.csv``) or, outside the NIFTY 200, Upstox's own name
for it from its published NSE instrument list. Only keys found in one of those
can be saved.
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting
from app.db.session import SessionLocal
from app.services.sectors import sector_of
from app.services.swing_universe import SwingUniverseError, nifty200

WATCHLIST_KEY = "intraday_watchlist"
NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
MASTER_TTL_SECONDS = 12 * 3600
MAX_SHARES = 100
PRE_OPEN_CUTOFF = (9, 0)  # IST: changes saved before this apply the same session

# The indices offered as the benchmark. Each is checked against Upstox's own
# list before it can be saved, so a misspelt key cannot reach the scanner.
BENCHMARKS: dict[str, str] = {
    "NSE_INDEX|Nifty 50": "NIFTY 50",
    "NSE_INDEX|Nifty 100": "NIFTY 100",
    "NSE_INDEX|Nifty 200": "NIFTY 200",
    "NSE_INDEX|Nifty 500": "NIFTY 500",
    "NSE_INDEX|Nifty Bank": "NIFTY BANK",
}


class WatchlistError(ValueError):
    pass


class SavedWatchlist(BaseModel):
    instruments: list[str] = Field(min_length=1, max_length=MAX_SHARES)
    benchmark: str


@dataclass(frozen=True)
class Listing:
    instrument_key: str
    symbol: str
    name: str
    in_nifty200: bool
    source: str  # "NIFTY 200 file" or "Upstox NSE list"


# --- reading and layering -----------------------------------------------------


async def load(session: AsyncSession) -> SavedWatchlist | None:
    row = await session.get(ApplicationSetting, WATCHLIST_KEY)
    if row is None or not isinstance(row.value, dict):
        return None
    try:
        return SavedWatchlist.model_validate(row.value)
    except ValueError:
        return None


def apply(settings: Settings, saved: SavedWatchlist | None) -> Settings:
    """``settings`` with the saved list and benchmark in place of the environment's."""
    if saved is None:
        return settings
    return settings.model_copy(
        update={"upstox_subscriptions": ",".join(saved.instruments), "upstox_nifty_benchmark_key": saved.benchmark}
    )


async def effective(settings: Settings) -> Settings:
    async with SessionLocal() as session:
        return apply(settings, await load(session))


def env_instruments(settings: Settings) -> list[str]:
    return [value.strip() for value in settings.upstox_subscriptions.split(",") if value.strip()]


def is_index(key: str) -> bool:
    return key.startswith(("NSE_INDEX|", "BSE_INDEX|"))


# --- the catalogue a share is picked from --------------------------------------

_master: dict[str, Any] = {"at": 0.0, "equities": {}, "indices": {}}


def _nifty200() -> dict[str, Listing]:
    try:
        members = nifty200()
    except SwingUniverseError:
        return {}
    return {m.instrument_token: Listing(m.instrument_token, m.symbol, m.name, True, "NIFTY 200 file") for m in members}


async def _upstox_master(force: bool = False) -> tuple[dict[str, Listing], dict[str, str]]:
    """Upstox's NSE equities and indices, downloaded at most every 12 hours. Empty if it cannot be fetched."""
    if not force and time.monotonic() - _master["at"] < MASTER_TTL_SECONDS and _master["equities"]:
        return _master["equities"], _master["indices"]
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.get(NSE_INSTRUMENTS_URL)
        response.raise_for_status()
        rows = json.loads(gzip.decompress(response.content))
    except (httpx.HTTPError, OSError, ValueError):
        return _master["equities"], _master["indices"]
    equities: dict[str, Listing] = {}
    indices: dict[str, str] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("instrument_key")
        if not isinstance(key, str):
            continue
        if item.get("segment") == "NSE_EQ" and item.get("instrument_type") == "EQ":
            symbol = str(item.get("trading_symbol") or "")
            equities[key] = Listing(key, symbol, str(item.get("name") or symbol), False, "Upstox NSE list")
        elif item.get("segment") == "NSE_INDEX":
            indices[key] = str(item.get("name") or key.split("|", 1)[-1])
    _master.update(at=time.monotonic(), equities=equities, indices=indices)
    return equities, indices


async def catalogue() -> dict[str, Listing]:
    """Every share that can be picked: the NIFTY 200 file first, then Upstox's list."""
    equities, _ = await _upstox_master()
    merged = dict(equities)
    merged.update(_nifty200())  # the official name wins where both have the share
    return merged


async def search(query: str, limit: int = 20) -> list[Listing]:
    text = query.strip().upper()
    if len(text) < 2:
        return []
    found: list[tuple[int, Listing]] = []
    for listing in (await catalogue()).values():
        symbol, name = listing.symbol.upper(), listing.name.upper()
        if symbol == text:
            rank = 0
        elif symbol.startswith(text):
            rank = 1
        elif text in name:
            rank = 2
        elif text in symbol:
            rank = 3
        else:
            continue
        found.append((rank if listing.in_nifty200 else rank + 4, listing))
    found.sort(key=lambda pair: (pair[0], pair[1].symbol))
    return [listing for _, listing in found[:limit]]


async def describe(keys: list[str], names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Each subscribed share with its name and sector group, for the screen."""
    known = await catalogue()
    rows = []
    for key in keys:
        listing = known.get(key)
        symbol = listing.symbol if listing else (names or {}).get(key, key)
        group = sector_of(symbol)
        rows.append(
            {
                "instrument_key": key,
                "symbol": symbol,
                "name": listing.name if listing else "Not found in the NSE lists — check this key",
                "in_nifty200": bool(listing and listing.in_nifty200),
                "known": listing is not None,
                "sector": None if group.startswith("UNMAPPED:") else group,
            }
        )
    return rows


async def validate(instruments: list[str], benchmark: str) -> SavedWatchlist:
    """Refuse anything that is not a known NSE equity, or a benchmark not on the list."""
    keys = list(dict.fromkeys(key.strip() for key in instruments if key.strip()))
    if not keys:
        raise WatchlistError("Keep at least one share in the watchlist.")
    if len(keys) > MAX_SHARES:
        raise WatchlistError(f"At most {MAX_SHARES} shares.")
    if benchmark not in BENCHMARKS:
        raise WatchlistError("Pick the benchmark from the list.")
    known = await catalogue()
    unknown = [key for key in keys if key not in known]
    if unknown:
        raise WatchlistError(f"Not an NSE equity this app can find: {', '.join(unknown[:5])}.")
    _, indices = await _upstox_master()
    if indices and benchmark not in indices:
        raise WatchlistError(f"Upstox does not list the index {BENCHMARKS[benchmark]}.")
    return SavedWatchlist(instruments=keys, benchmark=benchmark)
