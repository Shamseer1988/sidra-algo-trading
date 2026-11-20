"""The shares the swing desk looks at: the NIFTY 200.

Read from ``app/data/nifty200.csv``, the constituents file niftyindices.com
publishes (``ind_nifty200list.csv``), committed as it was downloaded on
20 November 2026. NSE rebalances the index twice a year; replacing the file
with the new one is the whole update.

Separate from the intraday watchlist on purpose. The intraday desk scans the
configured subscriptions; nothing here is subscribed to the live feed or
offered to the intraday scanner, and the intraday dynamic universe chooses its
candidates from the watchlist only.

The Upstox instrument key for an NSE equity is ``NSE_EQ|<ISIN>``, which is why
the file's ISIN is enough to name each share to the broker.

The file's ``Industry`` column is the swing desk's sector. The intraday desk's
sector map (``app/services/sectors.py``) is a different thing -- groups drawn
by co-movement, keyed by trading symbol -- and is not touched here.
"""

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

NIFTY200_FILE = Path(__file__).resolve().parents[1] / "data" / "nifty200.csv"
EXPECTED_MEMBERS = 200


class SwingUniverseError(RuntimeError):
    """The constituents file is missing, malformed, or not the size it says it is."""


@dataclass(frozen=True)
class SwingInstrument:
    symbol: str
    name: str
    industry: str
    isin: str

    @property
    def instrument_token(self) -> str:
        return f"NSE_EQ|{self.isin}"


def parse_constituents(lines: list[str], *, expected: int | None = EXPECTED_MEMBERS) -> tuple[SwingInstrument, ...]:
    """The members, in file order. Refuses a file that would quietly shrink the universe.

    A short or duplicated list is an error rather than a smaller universe: a
    download that came back truncated would otherwise remove shares from every
    scan without anyone noticing.
    """
    reader = csv.DictReader(lines)
    required = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
    if not reader.fieldnames or not required <= {name.strip() for name in reader.fieldnames}:
        raise SwingUniverseError(f"Constituents file is missing columns; expected {sorted(required)}")
    members: list[SwingInstrument] = []
    for row in reader:
        clean = {key.strip(): (value or "").strip() for key, value in row.items() if key}
        if clean.get("Series") != "EQ":
            continue
        members.append(
            SwingInstrument(
                symbol=clean["Symbol"],
                name=clean["Company Name"],
                industry=clean["Industry"],
                isin=clean["ISIN Code"],
            )
        )
    isins = [member.isin for member in members]
    if len(set(isins)) != len(isins):
        raise SwingUniverseError("Constituents file lists an ISIN more than once")
    if any(len(isin) != 12 or not isin.startswith("INE") for isin in isins):
        raise SwingUniverseError("Constituents file has an ISIN that is not an Indian equity ISIN")
    if expected is not None and len(members) != expected:
        raise SwingUniverseError(f"Constituents file has {len(members)} members, expected {expected}")
    return tuple(members)


@lru_cache(maxsize=1)
def nifty200() -> tuple[SwingInstrument, ...]:
    try:
        lines = NIFTY200_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise SwingUniverseError(f"Constituents file could not be read: {exc}") from exc
    return parse_constituents(lines)


def by_token() -> dict[str, SwingInstrument]:
    return {member.instrument_token: member for member in nifty200()}
