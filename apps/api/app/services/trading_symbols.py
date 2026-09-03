"""Mapping and dynamic resolution of market instruments to readable Script Names (Trading Symbols).

Supports standard Upstox keys (NSE_EQ|INE..., NSE_INDEX|Nifty 50), Firstock tokens (NSE:26000),
and dynamic resolution from persisted InstrumentMasterRefresh records.
"""

from __future__ import annotations

# Static mapping of confirmed Nifty 50 / Index ISINs and tokens to popular trading symbols
KNOWN_SCRIPT_SYMBOLS: dict[str, str] = {
    # Indices
    "NSE_INDEX|Nifty 50": "NIFTY 50",
    "NSE_INDEX|NIFTY 50": "NIFTY 50",
    "NSE_INDEX|Nifty Bank": "BANKNIFTY",
    "NSE_INDEX|NIFTY BANK": "BANKNIFTY",
    "NSE:26000": "NIFTY 50",
    "NSE:26001": "BANKNIFTY",
    "NSE:26009": "NIFTY IT",
    # Top Equities (ISIN & Tokens)
    "NSE_EQ|INE002A01018": "RELIANCE",
    "NSE:2885": "RELIANCE",
    "NSE_EQ|INE467B01029": "TCS",
    "NSE:11536": "TCS",
    "NSE_EQ|INE009A01021": "INFY",
    "NSE:1594": "INFY",
    "NSE_EQ|INE040A01034": "HDFCBANK",
    "NSE:1333": "HDFCBANK",
    "NSE_EQ|INE090A01021": "ICICIBANK",
    "NSE:4963": "ICICIBANK",
    "NSE_EQ|INE062A01020": "SBIN",
    "NSE:3045": "SBIN",
    "NSE_EQ|INE397D01024": "BHARTIARTL",
    "NSE:10604": "BHARTIARTL",
    "NSE_EQ|INE238A01034": "AXISBANK",
    "NSE:5900": "AXISBANK",
    "NSE_EQ|INE018A01030": "LT",
    "NSE:11483": "LT",
    "NSE_EQ|INE154A01025": "ITC",
    "NSE:1660": "ITC",
    "NSE_EQ|INE030A01027": "HINDUNILVR",
    "NSE:1394": "HINDUNILVR",
    "NSE_EQ|INE216A01030": "KOTAKBANK",
    "NSE:1922": "KOTAKBANK",
}


def resolve_script_name(instrument_token: str) -> str:
    """Return the human-readable Script Name (e.g. RELIANCE, NIFTY 50) for an instrument token.

    Falls back to parsing symbol from raw key or returning the raw key if unmapped.
    """
    if not instrument_token:
        return "UNKNOWN"

    # 1. Check known lookup table
    if instrument_token in KNOWN_SCRIPT_SYMBOLS:
        return KNOWN_SCRIPT_SYMBOLS[instrument_token]

    # 2. Extract name if key format is NSE_INDEX|Symbol or NSE_EQ|Symbol
    if "|" in instrument_token:
        parts = instrument_token.split("|", 1)
        suffix = parts[1].strip()
        # If suffix is not an ISIN (does not start with INE), it's likely a symbol name
        if not suffix.startswith("INE") and len(suffix) > 1:
            return suffix.upper()

    # 3. Extract prefix if format is NSE:SYMBOL
    if ":" in instrument_token:
        parts = instrument_token.split(":", 1)
        if not parts[1].isdigit():
            return parts[1].upper()

    return instrument_token
