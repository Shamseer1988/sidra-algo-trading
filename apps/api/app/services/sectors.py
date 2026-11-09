"""Which instruments move together, so the day cannot bet on one thing three times.

On 9 November the account opened four positions at 09:37:01. Three of them --
SBIN, PNB and BANKBARODA -- were PSU banks. That is not three trades. It is one
trade at three times the size, because those three stops do not fail
independently: one move in the PSU banking pack takes all of them.

The watchlist ranking cannot prevent it and should not try. It scores liquidity,
volatility, gap and trend (``universe.rank_universe``), and on a day the banks
are moving the banks genuinely *are* the best candidates -- most volume, cleanest
trends. Forcing one name per sector into the ranking would mean trading a share
that is not moving in place of one that is. The concentration is a risk to bound
at the moment of the trade, not a flaw in the selection.

**Grouped by what co-moves intraday, not by what a GICS table calls them.**
ADANIENT and ADANIPORTS are different industries and move on the same group
news. PFC, RECLTD and IRFC are three different businesses on paper and one
PSU-financing theme in practice. Private and public sector banks are kept in one
BANK group rather than split: they diverge on theme days and still all follow
BANKNIFTY, and a cap is worth more when it is drawn wide.

**An unmapped instrument is its own group.** It is never pooled with another,
so an out-of-date map can never cause a wrong refusal -- it only fails to
protect, and ``watchlist.py`` says so by name on every run. The map is small
and belongs to the watchlist; when one changes, so does the other.
"""

from __future__ import annotations

# Instrument -> group, by trading symbol. Confirmed with Shamseer on 9 November:
# IRFC sits with the PSU financiers rather than with the railway names, and
# IDFCFIRSTB stays in BANK.
SECTORS: dict[str, str] = {
    # Everything that follows BANKNIFTY. Seven of the twenty-seven.
    "AXISBANK": "BANK",
    "ICICIBANK": "BANK",
    "HDFCBANK": "BANK",
    "SBIN": "BANK",
    "BANKBARODA": "BANK",
    "PNB": "BANK",
    "IDFCFIRSTB": "BANK",
    # NIFTY METAL. Global commodity prices and China news move these together
    # more tightly than almost anything else on the list.
    "TATASTEEL": "METAL",
    "HINDALCO": "METAL",
    "JSWSTEEL": "METAL",
    "VEDL": "METAL",
    # The PSU power-financing pack. Three names, one theme.
    "PFC": "PSU_FIN",
    "RECLTD": "PSU_FIN",
    "IRFC": "PSU_FIN",
    # Railway capex.
    "RVNL": "RAILWAY",
    "IRCTC": "RAILWAY",
    # Defence orders.
    "HAL": "DEFENCE",
    "BEL": "DEFENCE",
    # A group, not a sector, and the strongest co-movement of the three.
    "ADANIENT": "ADANI",
    "ADANIPORTS": "ADANI",
    # One name each; grouped anyway so the map reads as a whole and a second
    # name in any of them has an obvious home.
    "BAJFINANCE": "NBFC",
    "TATAPOWER": "POWER",
    "M&M": "AUTO",
    "INFY": "IT",
    "RELIANCE": "ENERGY",
    "LT": "INFRA",
    "BHARTIARTL": "TELECOM",
}

# What a broker appends to a tradable name. Upstox reports the instrument token
# in this field and Firstock a name like "SBIN-EQ"; both reach here.
_SUFFIXES = ("-EQ", "-BE", "-BZ")


def normalise(symbol: str | None) -> str:
    """A trading symbol in the one spelling this map is keyed by."""
    if not symbol:
        return ""
    name = symbol.strip().upper()
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def sector_of(symbol: str | None) -> str:
    """The group this instrument belongs to.

    An unmapped name becomes a group of its own -- ``UNMAPPED:RVNL`` -- rather
    than sharing one ``UNKNOWN`` bucket with every other unmapped name. Sharing
    would pool two unrelated shares and refuse the second of them, which is a
    wrong refusal caused by a stale map; this way a stale map costs protection
    it was never given, and nothing else.
    """
    name = normalise(symbol)
    if not name:
        return "UNMAPPED:"
    return SECTORS.get(name, f"UNMAPPED:{name}")


def is_mapped(symbol: str | None) -> bool:
    return normalise(symbol) in SECTORS


def unmapped(symbols: list[str]) -> list[str]:
    """The names a watchlist carries that this map does not know, for a report."""
    return sorted({normalise(item) for item in symbols if item and not is_mapped(item)})


def members(group: str) -> list[str]:
    return sorted(name for name, value in SECTORS.items() if value == group)
