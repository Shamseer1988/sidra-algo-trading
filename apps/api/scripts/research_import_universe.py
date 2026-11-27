"""Record a universe's instruments in the research schema. Writes ``research.*`` only.

    docker compose exec -T research-worker python scripts/research_import_universe.py \\
        --file /app/research_data/universe.json --universe NIFTY200 --as-of 2026-11-27

The file is a JSON list of ``[instrument_key, symbol, industry]`` rows -- the
shape of the other session's ``~/sidra-research/data/universe.json``.

**This is today's list, not history.** Index membership changes every six
months, and the shares that left the NIFTY 200 (often because they fell) are not
in it. Each membership row says so in its ``note``, and a backtest over this
universe is survivorship-biased in the shares' favour until a dated history of
constituents is loaded. That history is not fabricated here.

Sectors come from ``app/services/sectors.py``, the map the scanner's per-sector
ceiling uses; an unmapped symbol keeps the file's industry and no sector.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import ResearchInstrument, ResearchUniverseMembership  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.sectors import is_mapped, sector_of  # noqa: E402

CURRENT_ONLY = "Current constituents only; historical membership unknown (survivorship bias)"


def parse_universe(rows: list) -> list[tuple[str, str, str | None]]:
    parsed = []
    for row in rows:
        if not isinstance(row, list | tuple) or len(row) < 2:
            raise ValueError(f"Expected [instrument_key, symbol, industry], got {row!r}")
        key, symbol = str(row[0]).strip(), str(row[1]).strip().upper()
        if "|" not in key or not symbol:
            raise ValueError(f"Not an instrument key and symbol: {row!r}")
        parsed.append((key, symbol, (str(row[2]).strip() or None) if len(row) > 2 else None))
    return parsed


async def import_universe(rows: list, universe: str, as_of: date, source: str) -> tuple[int, int]:
    """Upsert instruments and add membership rows. Returns (instruments, new memberships)."""
    parsed = parse_universe(rows)
    added = 0
    async with SessionLocal() as session:
        for key, symbol, industry in parsed:
            instrument = await session.get(ResearchInstrument, key)
            if instrument is None:
                instrument = ResearchInstrument(instrument_key=key, symbol=symbol, source=source)
                session.add(instrument)
            instrument.symbol = symbol
            instrument.industry = industry
            instrument.sector = sector_of(symbol) if is_mapped(symbol) else None
            instrument.source = source
            exists = await session.scalar(
                select(ResearchUniverseMembership.id).where(
                    ResearchUniverseMembership.universe == universe,
                    ResearchUniverseMembership.instrument_key == key,
                    ResearchUniverseMembership.valid_from == as_of,
                )
            )
            if exists is None:
                session.add(
                    ResearchUniverseMembership(
                        universe=universe,
                        instrument_key=key,
                        valid_from=as_of,
                        source=source,
                        note=CURRENT_ONLY,
                    )
                )
                added += 1
        await session.commit()
    return len(parsed), added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    rows = json.loads(args.file.read_text(encoding="utf-8"))
    count, added = asyncio.run(import_universe(rows, args.universe.upper(), args.as_of, f"file:{args.file.name}"))
    print(f"{count} instruments recorded; {added} new {args.universe.upper()} memberships as of {args.as_of}.")
    print(f"Note on every row: {CURRENT_ONLY}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
