"""Every instrument this deployment is watching, by name, and what it can trade.

"Which stocks are we scanning?" has no single answer anywhere in the UI. The
list starts as raw Upstox keys in ``UPSTOX_SUBSCRIPTIONS`` -- strings like
``NSE_EQ|INE002A01018``, which no one can read -- and is then narrowed at two
further points before a signal is possible: the benchmark and VIX are streamed
but never traded, and the share-price band refuses a signal whose entry sits
outside it.

So this resolves each key to its trading symbol and says, per instrument,
whether it is tradeable today and why not when it is not.

Read-only. It opens the database and reads the stored instrument master; it
asks the broker nothing and is safe during a live session.

Usage:

    docker compose exec api python scripts/watchlist.py
    docker compose exec api python scripts/watchlist.py --tokens   # keys too
"""

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import desc, select  # noqa: E402

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import (
    ApplicationSetting,  # noqa: E402
    MarketCandle,  # noqa: E402
)
from app.db.session import SessionLocal  # noqa: E402
from app.services.trading_symbols import instrument_tick_size, resolve_script_names  # noqa: E402
from app.services.upstox_market_data import configured_subscriptions, feed_subscriptions  # noqa: E402

RULE = "=" * 74


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens", action="store_true", help="Show the Upstox key beside each name.")
    args = parser.parse_args()

    settings = get_settings()
    configured = configured_subscriptions(settings)
    streamed = feed_subscriptions(settings)

    if not configured:
        print("UPSTOX_SUBSCRIPTIONS is empty, so nothing is streamed and nothing can be scanned.")
        return 1

    # Streamed but never traded. The benchmark and VIX are force-added to the
    # feed when they are absent, because relative strength and the NIFTY
    # regime silently score zero without them -- but an operator may also have
    # listed them in UPSTOX_SUBSCRIPTIONS by hand, as this deployment does.
    # Deciding by "was it force-added" therefore reported NIFTY as something
    # the scanner could signal on. It cannot: on_completed_candle returns
    # immediately for the benchmark token, so an index is never evaluated by
    # any strategy however it reached the feed. Decided by what the key is.
    index_keys = {key for key in (settings.upstox_nifty_benchmark_key, settings.upstox_india_vix_key) if key}

    def is_reference(key: str) -> bool:
        return key in index_keys or key.startswith("NSE_INDEX|") or key.startswith("BSE_INDEX|")

    equities = [key for key in configured if not is_reference(key)]
    reference = [key for key in streamed if is_reference(key)]

    if not equities:
        print("Every subscribed key is an index. Indices are never evaluated, so nothing can be scanned.")
        return 1

    async with SessionLocal() as session:
        names = await resolve_script_names(session, streamed)
        # Read before the watchlist is printed, because each row is now
        # measured against the band rather than the band being stated after.
        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS)
        high = Decimal(str(controls.universe_max_share_price))
        low = Decimal(str(controls.universe_min_share_price))
        ticks = {key: await instrument_tick_size(session, key) for key in equities}
        # The last completed candle this deployment stored, which is what the
        # band will be applied to on the next signal. Asking the broker would
        # be a live price; this is the price the system itself has, and a
        # disagreement between the two is its own finding.
        closes: dict[str, Decimal | None] = {}
        for key in equities:
            closes[key] = await session.scalar(
                select(MarketCandle.close)
                .where(MarketCandle.instrument_token == key)
                .order_by(desc(MarketCandle.opened_at))
                .limit(1)
            )

    heading(f"WATCHLIST — {len(equities)} instrument(s) the scanner can signal on")
    # Sized to the longest name actually present: an instrument the master
    # could not name falls back to its key, which is wider than any symbol and
    # would otherwise push every row after it out of line.
    width = max((len(names.get(key, key)) for key in streamed), default=18)
    blocked: list[str] = []
    unseen: list[str] = []
    for key in equities:
        name = names.get(key, key)
        tick = ticks.get(key)
        grid = f"tick ₹{tick}" if tick is not None else "tick unknown — a price may be refused"
        close = closes.get(key)
        if close is None:
            unseen.append(name)
            price = "no candle stored yet"
        else:
            price = f"last ₹{close:,.2f}"
            if (high > 0 and close > high) or (low > 0 and close < low):
                blocked.append(name)
                price += "  OUTSIDE THE BAND"
        print(f"  {name:<{width}}  {grid:<16}  {price}" + (f"   {key}" if args.tokens else ""))

    if reference:
        heading(f"REFERENCE — {len(reference)} streamed for scoring, never traded")
        for key in reference:
            print(f"  {names.get(key, key):<{width}}  " + (key if args.tokens else ""))

    heading("WHAT STILL NARROWS THIS")
    if not settings.universe_enabled:
        print("  universe_enabled is off, so every instrument above is scanned every candle.")
        print("  The turnover, ATR and liquidity ranking is not running.")
    else:
        print("  universe_enabled is on, so the daily ranking picks a subset of the above.")
        print("  Run the Universe screen to see today's selection; this list is the pool it draws from.")
    band = []
    if high > 0:
        band.append(f"above ₹{high:,.2f}")
    if low > 0:
        band.append(f"below ₹{low:,.2f}")
    if band:
        print(f"  A signal is refused when its entry price is {' or '.join(band)}.")
        print("  That is checked per signal on the live price, so a name here can still be skipped today.")
    else:
        print("  No share-price band is set, so price alone never refuses a signal.")

    if blocked:
        print()
        print(f"  [WARN] {len(blocked)} instrument(s) last traded outside the band:")
        print(f"         {', '.join(sorted(blocked))}")
        print("         They are scanned every candle and a signal on them is refused on price.")
        print("         Raise the band, or drop them from UPSTOX_SUBSCRIPTIONS.")
    if unseen:
        print()
        print(f"  [WARN] {len(unseen)} instrument(s) have no stored candle at all:")
        print(f"         {', '.join(sorted(unseen))}")
        print("         Either they were subscribed only just now, or the feed is not reaching them.")

    unknown = [names.get(key, key) for key, tick in ticks.items() if tick is None]
    if unknown:
        print()
        print(f"  [WARN] {len(unknown)} instrument(s) have no tick size in the stored master:")
        print(f"         {', '.join(sorted(unknown))}")
        print("         Their orders fall back to ₹0.05 and may be refused on price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
