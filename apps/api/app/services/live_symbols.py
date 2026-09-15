"""Translate an internal instrument token into a Firstock order symbol.

Phase 3 of the live-execution layer. This module exists because
``trading_symbols.resolve_script_name`` must never be used to build an order.
That function is a display helper: when it cannot resolve a token it returns the
token unchanged, and when it resolves an index it returns a human label like
``NIFTY 50``. Both behaviours are correct for a table cell and catastrophic for a
live order, where an unrecognised string is either rejected by the broker or,
far worse, matches a different instrument than the one we analysed.

So this translation has exactly two outcomes: a symbol we can defend, or a
refusal. It never falls back to the raw token, never strips characters until
something matches, and never infers a symbol from the shape of a string.

Resolution comes from two sources only:

    1. The static equity map in ``trading_symbols``, which is hand-verified.
    2. The persisted Upstox instrument master, which the broker's own refresh
       produced.

Anything else refuses, and the refusal is the useful output: it names an
instrument the scanner is trading on paper that we could not safely name at the
broker, which is something to fix before going live rather than after.

Indices refuse unconditionally. The scanner uses NIFTY as a benchmark for
relative strength, and a benchmark token reaching an order payload would try to
buy the index as though it were a share.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.trading_symbols import KNOWN_SCRIPT_SYMBOLS, instrument_master_symbols

# NSE cash-market equity series. Firstock addresses equities as ``SYMBOL-EQ``.
EQUITY_SERIES_SUFFIX = "-EQ"

NSE_EXCHANGE = "NSE"

# Resolved names that are indices rather than tradeable equities. Kept as names
# rather than tokens so that a new index token routing through the static map or
# the instrument master is caught by the same check.
INDEX_NAMES = frozenset(
    {
        "NIFTY 50",
        "NIFTY50",
        "NIFTY",
        "BANKNIFTY",
        "NIFTY BANK",
        "NIFTY IT",
        "FINNIFTY",
        "MIDCPNIFTY",
        "SENSEX",
        "BANKEX",
    }
)


@dataclass(frozen=True)
class SymbolTranslation:
    """A defensible order symbol, or a refusal that says why."""

    resolved: bool
    reason: str
    exchange: str | None = None
    trading_symbol: str | None = None

    @property
    def status(self) -> str:
        return "RESOLVED" if self.resolved else "UNRESOLVED"


def _refuse(reason: str) -> SymbolTranslation:
    return SymbolTranslation(resolved=False, reason=reason)


def _to_equity_symbol(base: str) -> str:
    """``RELIANCE`` becomes ``RELIANCE-EQ``; an already-suffixed name is left alone."""
    upper = base.strip().upper()
    return upper if upper.endswith(EQUITY_SERIES_SUFFIX) else f"{upper}{EQUITY_SERIES_SUFFIX}"


def _exchange_for(instrument_token: str) -> str | None:
    """Only NSE cash is supported today, and only when the token says so.

    Returning None for anything else is deliberate. The reference documentation
    lists NSE, BSE and NFO followed by "etc.", so an exhaustive exchange list
    cannot be derived from it, and guessing an exchange for a symbol is how an
    order reaches the wrong book.
    """
    if instrument_token.startswith(("NSE_EQ|", "NSE:")):
        return NSE_EXCHANGE
    return None


async def translate_for_order(session: AsyncSession, instrument_token: str) -> SymbolTranslation:
    """Resolve one instrument token to a Firstock equity symbol, or refuse."""
    token = (instrument_token or "").strip()
    if not token:
        return _refuse("No instrument token was supplied.")

    exchange = _exchange_for(token)
    if exchange is None:
        return _refuse(f"{token} does not identify a supported exchange; only NSE cash is mapped.")

    base = KNOWN_SCRIPT_SYMBOLS.get(token)
    source = "the verified static map"
    if base is None:
        master = await instrument_master_symbols(session)
        base = master.get(token)
        source = "the persisted instrument master"

    if base is None:
        return _refuse(
            f"{token} is not present in the verified symbol map or the instrument master. "
            "Add a verified mapping before this instrument can trade live."
        )

    if base.strip().upper() in INDEX_NAMES:
        return _refuse(f"{token} resolves to the index {base}, which cannot be traded as an equity order.")

    return SymbolTranslation(
        resolved=True,
        reason=f"Resolved from {source}.",
        exchange=exchange,
        trading_symbol=_to_equity_symbol(base),
    )
