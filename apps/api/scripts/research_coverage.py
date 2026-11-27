"""What each research dataset actually holds, by instrument and session. Read-only.

    docker compose exec -T api python scripts/research_coverage.py
    docker compose exec -T api python scripts/research_coverage.py --dataset nifty200-1m-2022 --worst 15

A session is a day the dataset's benchmark (NIFTY 50) has bars, and its expected
bar count is the benchmark's that day. "Complete" means every expected bar is
present. Missing data is reported as missing; nothing is filled in.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, true  # noqa: E402

from app.db.models import ResearchDataset, ResearchDatasetCoverage  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def summarise(rows: list) -> dict:
    """Per-instrument totals from coverage rows (instrument_key, expected, bars, missing, dup, bad, zero, outside)."""
    totals: dict[str, dict] = {}
    for key, expected, bars, missing, duplicates, bad, zero, outside in rows:
        item = totals.setdefault(
            key,
            {
                "sessions": 0,
                "complete": 0,
                "absent": 0,
                "missing": 0,
                "duplicates": 0,
                "bad_ohlc": 0,
                "zero_volume": 0,
                "outside": 0,
            },
        )
        if expected > 0:
            item["sessions"] += 1
            item["complete"] += missing == 0
            item["absent"] += bars == 0
        item["missing"] += missing
        item["duplicates"] += duplicates
        item["bad_ohlc"] += bad
        item["zero_volume"] += zero
        item["outside"] += outside
    return totals


async def report(name: str | None, worst: int) -> int:
    async with SessionLocal() as session:
        datasets = list(
            (
                await session.scalars(
                    select(ResearchDataset)
                    .where(ResearchDataset.name == name if name else true())
                    .order_by(ResearchDataset.created_at)
                )
            ).all()
        )
        if not datasets:
            print("No research datasets." if not name else f"No dataset named {name}.")
            return 1
        for dataset in datasets:
            span = await session.execute(
                select(
                    func.min(ResearchDatasetCoverage.session_date), func.max(ResearchDatasetCoverage.session_date)
                ).where(ResearchDatasetCoverage.dataset_id == dataset.id, ResearchDatasetCoverage.expected_bars > 0)
            )
            first, last = span.one()
            rows = (
                await session.execute(
                    select(
                        ResearchDatasetCoverage.instrument_key,
                        ResearchDatasetCoverage.expected_bars,
                        ResearchDatasetCoverage.bars,
                        ResearchDatasetCoverage.missing_bars,
                        ResearchDatasetCoverage.duplicates,
                        ResearchDatasetCoverage.bad_ohlc,
                        ResearchDatasetCoverage.zero_volume,
                        ResearchDatasetCoverage.outside_session,
                    ).where(ResearchDatasetCoverage.dataset_id == dataset.id)
                )
            ).all()
            totals = summarise(rows)
            print("=" * 92)
            print(
                f"{dataset.name}  [{dataset.status}]  {dataset.timeframe}  {dataset.source}  prices {dataset.price_adjustment}"
            )
            print(
                f"  requested {dataset.start_date} .. {dataset.end_date}; sessions with benchmark bars {first} .. {last}"
            )
            print(
                f"  {dataset.instrument_count} instruments, {dataset.row_count:,} bars, sha256 {dataset.content_sha256 or '(not finalised)'}"
            )
            if not totals:
                print("  No coverage computed yet.")
                continue
            sessions = max(item["sessions"] for item in totals.values())
            complete = sum(item["complete"] for item in totals.values())
            possible = sum(item["sessions"] for item in totals.values())
            print(
                f"  {sessions} sessions; {complete:,} of {possible:,} instrument-sessions complete ({complete / max(possible, 1):.1%})"
            )
            print(
                f"  duplicates {sum(i['duplicates'] for i in totals.values()):,}  "
                f"bad OHLC {sum(i['bad_ohlc'] for i in totals.values()):,}  "
                f"zero volume {sum(i['zero_volume'] for i in totals.values()):,}  "
                f"outside 09:15-15:30 {sum(i['outside'] for i in totals.values()):,}"
            )
            ranked = sorted(totals.items(), key=lambda kv: (kv[1]["complete"] / max(kv[1]["sessions"], 1), kv[0]))
            print(f"\n  {worst} least complete instruments:")
            print(f"  {'instrument':<28}{'sessions':>9}{'complete':>9}{'absent':>8}{'missing':>9}{'dup':>6}{'bad':>6}")
            for key, item in ranked[:worst]:
                print(
                    f"  {key:<28}{item['sessions']:>9}{item['complete']:>9}{item['absent']:>8}"
                    f"{item['missing']:>9}{item['duplicates']:>6}{item['bad_ohlc']:>6}"
                )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset")
    parser.add_argument("--worst", type=int, default=10)
    args = parser.parse_args()
    return asyncio.run(report(args.dataset, args.worst))


if __name__ == "__main__":
    raise SystemExit(main())
