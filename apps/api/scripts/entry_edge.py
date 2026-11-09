"""What separated the entries that worked from the entries that did not.

``trade_patterns.py`` said *what* the record looks like: 45% stopped, 25% reached
the target, 25% were still open at square-off, and 33 of 40 entries were taken in
the first half hour. None of that says *which* entries were the good ones, and
"fix the entry logic" cannot be answered by changing a threshold and hoping.

So this pairs each trade with the signal that produced it -- the score the
scanner gave it, the components that score was built from, and every number the
indicator snapshot carried at that candle -- and splits the record at each one's
median. A condition that matters shows a different win rate and a different
average R on the two sides of its own median. A condition that does not shows
the same thing twice, and is costing a threshold's worth of complexity for
nothing.

Three honest limits, stated here rather than discovered later:

  * **A median split of forty trades is twenty against twenty.** The 95%
    interval on a win rate of twenty trades is about plus or minus 22 points.
    Nothing here is evidence on its own; it says which hypothesis is worth a
    sweep, and a sweep over sixty sessions is what settles it.
  * **mfe_r is censored above the target.** A trade that reached 2R was closed
    there and never measured higher. So the excursion table reads honestly
    *below* 2R -- which is the half the entry question lives in -- and says
    nothing about how much further the winners could have run.
  * **Costs are in.** Every R here is net of charges, which is the only R that
    decides anything.

Read-only. It cannot place, cancel or modify an order, and writes nothing.

    docker compose exec api python scripts/entry_edge.py
    docker compose exec api python scripts/entry_edge.py --from 2026-10-07
    docker compose exec api python scripts/entry_edge.py --days 60 --ledger paper
    docker compose exec api python scripts/entry_edge.py --min-samples 8
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import PaperSignal, PaperSignalOutcome  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.trade_history import LIVE, PAPER, load_trades  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE as MARKET_TZ  # noqa: E402

RULE = "=" * 96
THIN = "-" * 96
ZERO = Decimal("0")

# Excursion bands. Narrow near zero because that is where the question is: an
# entry whose price never moved half a risk unit in its favour was not a trade
# that missed its target, it was a trade that never started.
BANDS = (
    (0.0, 0.25),
    (0.25, 0.5),
    (0.5, 1.0),
    (1.0, 1.5),
    (1.5, 2.0),
    (2.0, 99.0),
)

# Absolute price and bookkeeping levels. They are numbers, and splitting the
# record at the median share price would produce a confident-looking row that
# says expensive shares did better -- which is a fact about which shares the
# ranking picked, not about an entry condition.
NOT_A_CONDITION = {
    "computed_at",
    "last_close",
    "vwap",
    "ema_fast",
    "ema_slow",
    "atr",
    "daily_atr",
    "current_volume",
    "average_volume",
    "open",
    "high",
    "low",
    "close",
    "upper_1",
    "lower_1",
    "upper_2",
    "lower_2",
    "sigma",
    "previous_close",
}


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def leaf(path: str) -> str:
    return path.rpartition(".")[2]


def flatten(value: object, prefix: str = "") -> dict[str, float]:
    """Every numeric leaf of a snapshot, by dotted path.

    Written generically rather than against a list of known keys so a new
    indicator starts being measured the day it is added, instead of the day
    somebody remembers to add it here.
    """
    found: dict[str, float] = {}
    if isinstance(value, dict):
        for key, inner in value.items():
            found.update(flatten(inner, f"{prefix}.{key}" if prefix else str(key)))
        return found
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, int | float | Decimal):
        if leaf(prefix) not in NOT_A_CONDITION:
            found[prefix] = float(value)
        return found
    return found


@dataclass
class Entry:
    """One trade, with what was known about it at the moment it was entered."""

    name: str
    session_date: date
    side: str
    strategy: str
    entered_at: datetime | None
    net_r: float | None
    won: bool
    score: int | None
    mfe_r: float | None
    # Split out rather than merged into one bag: an operator reading D1 is asking
    # whether the scoring is earning its weights, and D2 whether the market was
    # in a state worth entering. Merged, neither question has its own table.
    components: dict[str, float]
    conditions: dict[str, float]


@dataclass(frozen=True)
class Bucket:
    samples: int
    wins: int
    average_r: float

    @property
    def win_percent(self) -> float:
        return self.wins * 100 / self.samples if self.samples else 0.0


def bucket(entries: list[Entry]) -> Bucket:
    measured = [item.net_r for item in entries if item.net_r is not None]
    return Bucket(
        samples=len(entries),
        wins=sum(1 for item in entries if item.won),
        average_r=(sum(measured) / len(measured)) if measured else 0.0,
    )


def excursion(entries: list[Entry]) -> None:
    print(RULE)
    print("A. HOW FAR PRICE WENT IN OUR FAVOUR AFTER ENTRY")
    print(RULE)
    measured = [item for item in entries if item.mfe_r is not None]
    if not measured:
        print("  No trade in this range carries a measured excursion.")
        print("  (mfe_r is written by the paper journal as later candles arrive. A range with")
        print("   no candles stored for it -- or an outage -- leaves these empty.)")
        print()
        return

    print("  mfe_r is the best unrealised R the trade ever showed, net of nothing: it is price,")
    print("  not money. The question it answers is whether the entry was into movement at all.")
    print()
    print(f"  {'best R reached':<18}{'trades':>8}{'share':>8}{'cumulative':>12}")
    running = 0
    for low, high in BANDS:
        inside = [item for item in measured if low <= (item.mfe_r or 0.0) < high]
        running += len(inside)
        label = f"{low:.2f} - {high:.2f}" if high < 99 else f"{low:.2f} and above"
        print(
            f"  {label:<18}{len(inside):>8}{len(inside) * 100 / len(measured):>7.0f}%"
            f"{running * 100 / len(measured):>11.0f}%"
        )
    print(THIN)
    stillborn = [item for item in measured if (item.mfe_r or 0.0) < 0.5]
    reached_one = [item for item in measured if (item.mfe_r or 0.0) >= 1.0]
    print(
        f"  {len(stillborn)} of {len(measured)} entries ({len(stillborn) * 100 / len(measured):.0f}%) "
        "never showed even half a risk unit."
    )
    print(
        f"  {len(reached_one)} of {len(measured)} ({len(reached_one) * 100 / len(measured):.0f}%) "
        "reached 1.0R at some point."
    )
    print("  A target is only reachable for the second group, and no target setting reaches the")
    print("  first. If the first group is the larger one, the entry is the thing to change --")
    print("  not the reward multiple, not the trailing rule, not the stop.")
    print()


def by_label(entries: list[Entry], title: str, key) -> None:  # noqa: ANN001
    print(RULE)
    print(title)
    print(RULE)
    grouped: dict[str, list[Entry]] = defaultdict(list)
    for item in entries:
        grouped[key(item)].append(item)
    print(f"  {'group':<28}{'trades':>8}{'won':>6}{'win%':>7}{'avg R':>9}{'mfe>=1R':>9}")
    for name, items in sorted(grouped.items(), key=lambda row: -len(row[1])):
        stats = bucket(items)
        measured = [row for row in items if row.mfe_r is not None]
        ran = sum(1 for row in measured if (row.mfe_r or 0.0) >= 1.0)
        share = f"{ran * 100 / len(measured):.0f}%" if measured else "—"
        print(
            f"  {name:<28}{stats.samples:>8}{stats.wins:>6}{stats.win_percent:>6.0f}%"
            f"{stats.average_r:>9.2f}{share:>9}"
        )
    print()


@dataclass(frozen=True)
class Split:
    name: str
    threshold: float
    below: Bucket
    above: Bucket

    @property
    def lift(self) -> float:
        return self.above.average_r - self.below.average_r

    @property
    def thin(self) -> bool:
        return min(self.below.samples, self.above.samples) < 8


def splits(entries: list[Entry], pick, minimum: int) -> list[Split]:  # noqa: ANN001
    """Each condition split at its own median across the trades that carry it."""
    names: set[str] = set()
    for item in entries:
        names.update(pick(item))
    found: list[Split] = []
    for name in sorted(names):
        carrying = [item for item in entries if name in pick(item)]
        if len(carrying) < minimum:
            continue
        threshold = median(pick(item)[name] for item in carrying)
        below = [item for item in carrying if pick(item)[name] < threshold]
        above = [item for item in carrying if pick(item)[name] >= threshold]
        if not below or not above:
            # Every trade on one side of its own median: a constant, or a value
            # the strategy itself gates on, so there is nothing to compare.
            continue
        found.append(Split(name, float(threshold), bucket(below), bucket(above)))
    return found


def report_splits(found: list[Split], title: str, note: str) -> None:
    print(RULE)
    print(title)
    print(RULE)
    if not found:
        print("  Nothing here carried enough trades to split. Lower --min-samples, or widen")
        print("  the date range.")
        print()
        return
    print(f"  {note}")
    print()
    print(
        f"  {'condition':<34}{'median':>9}   {'below: n  win%   avgR':<24}{'above: n  win%   avgR':<24}{'lift':>7}"
    )
    for item in sorted(found, key=lambda row: -abs(row.lift)):
        below = f"{item.below.samples:>3}  {item.below.win_percent:>4.0f}%  {item.below.average_r:>6.2f}"
        above = f"{item.above.samples:>3}  {item.above.win_percent:>4.0f}%  {item.above.average_r:>6.2f}"
        flag = " *" if item.thin else "  "
        print(f"  {item.name:<34}{item.threshold:>9.3f}   {below:<24}{above:<24}{item.lift:>+7.2f}{flag}")
    print(THIN)
    if any(item.thin for item in found):
        print("  * fewer than 8 trades on one side. Read those rows as a question, not an answer.")
    print("  'lift' is the average R above the median minus the average R below it. A condition")
    print("  worth gating on has a large lift AND enough trades on both sides AND a reason it")
    print("  should work. Two of the three is how a curve gets fitted.")
    print()


def ranked(score: list[Split], condition: list[Split]) -> None:
    print(RULE)
    print("E. WHAT TO TEST NEXT")
    print(RULE)
    everything = sorted(score + condition, key=lambda row: -abs(row.lift))
    usable = [item for item in everything if not item.thin]
    if not usable:
        print("  Every split here is thin. The record is too short to point anywhere; keep")
        print("  trading the current settings and run this again with more sessions.")
        print()
        return
    print("  The widest separators that are not thin, strongest first:")
    print()
    for item in usable[:6]:
        direction = "above" if item.lift > 0 else "below"
        better = item.above if item.lift > 0 else item.below
        worse = item.below if item.lift > 0 else item.above
        print(f"  {item.name}  —  {direction} {item.threshold:.3f}")
        print(
            f"      {better.samples} trades at {better.win_percent:.0f}% and {better.average_r:+.2f}R, "
            f"against {worse.samples} at {worse.win_percent:.0f}% and {worse.average_r:+.2f}R."
        )
    print()
    print("  Each of these is a hypothesis, not a setting to change. The next step for any one")
    print("  of them is a sweep over sixty sessions with that condition as the only variable,")
    print("  and an out-of-sample half that was not used to find it. A threshold adopted")
    print("  straight off this table is fitted to forty trades and will not survive the next")
    print("  forty.")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=45, help="How far back to look. Ignored if --from is given.")
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session. Default: today in IST.")
    parser.add_argument("--ledger", choices=("live", "paper"), default="live", help="Which account. Default live.")
    parser.add_argument(
        "--min-samples", type=int, default=12, help="Fewest trades a condition needs before it is split. Default 12."
    )
    args = parser.parse_args()

    to_date = args.to_date or datetime.now(MARKET_TZ).date()
    from_date = args.from_date or (to_date - timedelta(days=max(args.days, 1)))

    async with SessionLocal() as session:
        records = await load_trades(
            session, from_date, to_date, execution_mode=LIVE if args.ledger == "live" else PAPER
        )
        signal_ids = [record.signal_id for record in records if record.signal_id is not None]
        signals = {}
        outcomes = {}
        if signal_ids:
            rows = await session.scalars(select(PaperSignal).where(PaperSignal.id.in_(signal_ids)))
            signals = {row.id: row for row in rows.all()}
            rows = await session.scalars(
                select(PaperSignalOutcome).where(PaperSignalOutcome.paper_signal_id.in_(signal_ids))
            )
            outcomes = {row.paper_signal_id: row for row in rows.all()}

    entries: list[Entry] = []
    for record in records:
        signal = signals.get(record.signal_id)
        outcome = outcomes.get(record.signal_id)
        measured = record.r_multiple
        conditions = flatten(signal.indicator_snapshot) if signal else {}
        components = (
            {
                str(key): float(value)
                for key, value in (signal.score_breakdown or {}).items()
                if isinstance(value, int | float | Decimal) and not isinstance(value, bool)
            }
            if signal
            else {}
        )
        item = Entry(
            name=record.script_name,
            session_date=record.session_date,
            side=record.side.upper(),
            strategy=record.strategy_version.split("@")[0],
            entered_at=record.opened_at,
            net_r=float(measured) if measured is not None else None,
            won=record.net_pnl > ZERO,
            score=signal.score if signal else None,
            mfe_r=float(outcome.mfe_r) if outcome is not None and outcome.mfe_r is not None else None,
            components=components,
            conditions=conditions,
        )
        entries.append(item)

    print(RULE)
    print(f"ENTRY EDGE — {from_date} to {to_date}   ({args.ledger})")
    print(RULE)
    if not entries:
        print("  No trade in this range.")
        print(RULE)
        return 0
    with_signal = sum(1 for item in entries if item.score is not None)
    with_mfe = sum(1 for item in entries if item.mfe_r is not None)
    days = sorted({item.session_date for item in entries})
    print(f"  {len(entries)} trade(s) across {len(days)} session(s), {days[0]} to {days[-1]}.")
    print(f"  {with_signal} carry the signal that produced them; {with_mfe} carry a measured excursion.")
    if with_signal < len(entries):
        print("  A trade with no signal behind it is one placed outside the scanner, or one whose")
        print("  signal row was purged. It is counted above and left out of the splits below.")
    print()

    excursion(entries)
    by_label(entries, "B. BY SIDE — is the edge one-directional?", lambda item: item.side)
    by_label(entries, "C. BY STRATEGY — which one is carrying the record?", lambda item: item.strategy)

    score_splits = splits(entries, lambda item: item.components, args.min_samples)
    condition_splits = splits(entries, lambda item: item.conditions, args.min_samples)

    report_splits(
        score_splits,
        "D1. SCORE COMPONENTS — each split at its own median",
        "What the scanner thought it was rewarding, against what those rewards were worth.",
    )
    report_splits(
        condition_splits,
        "D2. CONDITIONS AT ENTRY — each split at its own median",
        "Every number the indicator snapshot carried at the entry candle. Absolute prices are left out.",
    )
    ranked(score_splits, condition_splits)

    print(RULE)
    print("  Counts and averages only. Nothing here was changed, and this script cannot place,")
    print("  cancel or modify an order.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
