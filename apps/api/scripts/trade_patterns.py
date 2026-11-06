"""What the record is actually doing, across days rather than within one.

``daily_review.py`` answers "what happened today". Four questions it cannot
answer are about the shape of the record itself, and each has been asked out
of a real suspicion:

  * the same stock twice in one day -- is that a mistake, or two strategies
    agreeing, or a long and a short?
  * the same handful of stocks every day -- a universe of 28 that in practice
    trades four of them is four bets, not twenty-eight.
  * "it only ever hits the stop" -- an impression formed from recent days,
    which the counts either confirm or refuse.
  * whether the hour a trade is entered predicts anything, which is the only
    honest way to pick a start time.

Each section reports counts, not advice. A stock that lost twice is two
trades: that is a fact about two trades and not yet a fact about the stock.

Read-only, and safe during a session.

    docker compose exec api python scripts/trade_patterns.py
    docker compose exec api python scripts/trade_patterns.py --days 30
    docker compose exec api python scripts/trade_patterns.py --from 2026-10-19
    docker compose exec api python scripts/trade_patterns.py --ledger paper
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls  # noqa: E402
from app.db.models import ApplicationSetting  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.exit_quality import LABELS, shape  # noqa: E402
from app.services.trade_history import LIVE, PAPER, load_trades  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE as MARKET_TZ  # noqa: E402

RULE = "=" * 96
THIN = "-" * 96
ZERO = Decimal("0")
# Half-hour buckets from the open. Narrower than an hour because the entry
# window is two hours wide, and wider than a candle because four trades in a
# bucket is already a thin number to read anything from.
BUCKET_MINUTES = 30


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def bucket_of(moment: time) -> time:
    minute = (moment.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    return time(moment.hour, minute)


def r_of(record) -> Decimal | None:  # noqa: ANN001 - a TradeRecord
    """Net R, from the money and the risk the trade was sized for."""
    if not record.risk_amount:
        return None
    return record.net_pnl / record.risk_amount


def by_stock(records: list) -> None:  # noqa: ANN001
    print(RULE)
    print("A. BY STOCK — is the universe of 28 really trading 28?")
    print(RULE)
    grouped: dict[str, list] = defaultdict(list)
    for record in records:
        grouped[record.script_name].append(record)

    rows = []
    for name, items in grouped.items():
        net = sum((item.net_pnl for item in items), start=ZERO)
        wins = sum(1 for item in items if item.net_pnl > 0)
        days = len({item.session_date for item in items})
        rows.append((name, len(items), days, wins, net))
    rows.sort(key=lambda row: (-row[1], row[0]))

    traded = len(rows)
    total = sum(row[1] for row in rows)
    print(f"  {'stock':<14}{'trades':>7}{'days':>6}{'won':>5}{'lost':>6}{'win%':>7}{'net':>12}{'share':>8}")
    for name, count, days, wins, net in rows:
        print(
            f"  {name:<14}{count:>7}{days:>6}{wins:>5}{count - wins:>6}"
            f"{wins * 100 / count:>6.0f}%{money(net):>12}{count * 100 / total:>7.0f}%"
        )
    print(THIN)
    print(f"  {traded} stock(s) traded out of the watchlist, {total} trade(s) in all.")
    if traded:
        top = rows[: min(3, traded)]
        share = sum(row[1] for row in top) * 100 / total
        names = ", ".join(row[0] for row in top)
        print(f"  The busiest {len(top)}: {names} — {share:.0f}% of every trade taken.")
    print("  Concentration is not itself a fault: the ranking is meant to pick the same")
    print("  liquid, volatile names on similar days. It is a fault if those names are")
    print("  also the losing ones, which the net column above says.")
    print()


def same_day_repeats(records: list) -> None:  # noqa: ANN001
    print(RULE)
    print("B. THE SAME STOCK TWICE IN A DAY — what happened, and was it allowed?")
    print(RULE)
    grouped: dict[tuple[date, str], list] = defaultdict(list)
    for record in records:
        grouped[(record.session_date, record.script_name)].append(record)
    repeats = {key: items for key, items in grouped.items() if len(items) > 1}

    if not repeats:
        print("  No stock was entered more than once in the same session.")
        print()
        return

    for (day, name), items in sorted(repeats.items()):
        print(f"  {day}  {name}  — {len(items)} entries")
        for item in sorted(items, key=lambda row: row.opened_at or datetime.min):
            opened = item.opened_at.astimezone(MARKET_TZ).strftime("%H:%M") if item.opened_at else "—"
            closed = item.closed_at.astimezone(MARKET_TZ).strftime("%H:%M") if item.closed_at else "open"
            measured = r_of(item)
            print(
                f"      {opened}->{closed}  {item.side:<5} {item.strategy_version:<22}"
                f" qty {item.quantity:<5} net {money(item.net_pnl):>9}"
                f"  {f'{measured:.2f}R' if measured is not None else '   —':>7}"
            )
        strategies = {item.strategy_version.split("@")[0] for item in items}
        sides = {item.side.upper() for item in items}
        if len(strategies) > 1:
            print("      -> two different strategies agreed on this stock. Each has its own")
            print("         per-day ceiling, and neither knows what the other took.")
        elif len(sides) > 1:
            print("      -> one long and one short, so the second is a reversal, not a repeat.")
        else:
            print("      -> same strategy, same side, twice. Check its cooldown_minutes and")
            print("         max_trades_per_day: this is the combination those settings bound.")
    print()
    print(f"  {len(repeats)} stock-day(s) with more than one entry.")
    print("  Two entries on one stock is two trades' worth of risk on one company's news.")
    print()


def how_they_ended(records: list, square_off: time | None) -> None:  # noqa: ANN001
    print(RULE)
    print("C. HOW TRADES ENDED — testing 'it only ever hits the stop'")
    print(RULE)
    closed = [record for record in records if not record.is_open]
    if not closed:
        print("  No closed trade in this range.")
        print()
        return

    shapes = [shape(record, square_off=square_off) for record in closed]
    counts: dict[str, list] = defaultdict(list)
    for item in shapes:
        counts[item.verdict].append(item)

    print(f"  {'ended by':<14}{'trades':>7}{'share':>8}   what it means")
    for verdict, items in sorted(counts.items(), key=lambda row: -len(row[1])):
        print(f"  {verdict:<14}{len(items):>7}{len(items) * 100 / len(shapes):>7.0f}%   {LABELS[verdict]}")
    print(THIN)

    wins = [item for item in shapes if item.is_win]
    print(
        f"  {len(wins)} of {len(shapes)} trades finished in profit before charges "
        f"({len(wins) * 100 / len(shapes):.0f}%)."
    )
    print("  'Always stopped out' and 'stopped out more often than not' are different")
    print("  claims, and only the second can be true of a strategy that still has an edge.")
    print()


def by_entry_time(records: list) -> None:  # noqa: ANN001
    print(RULE)
    print("D. BY ENTRY TIME — does the half-hour a trade is taken in predict anything?")
    print(RULE)
    buckets: dict[time, list] = defaultdict(list)
    for record in records:
        if record.opened_at is None:
            continue
        buckets[bucket_of(record.opened_at.astimezone(MARKET_TZ).time())].append(record)

    if not buckets:
        print("  No trade carries an entry time in this range.")
        print()
        return

    print(f"  {'entered':<10}{'trades':>7}{'won':>5}{'lost':>6}{'win%':>7}{'net':>12}{'avg R':>9}")
    for start in sorted(buckets):
        items = buckets[start]
        wins = sum(1 for item in items if item.net_pnl > 0)
        net = sum((item.net_pnl for item in items), start=ZERO)
        measured = [value for value in (r_of(item) for item in items) if value is not None]
        average = (sum(measured, start=ZERO) / len(measured)) if measured else None
        window = f"{start.strftime('%H:%M')}-{(datetime.combine(date.today(), start) + timedelta(minutes=BUCKET_MINUTES)).strftime('%H:%M')}"
        print(
            f"  {window:<10}{len(items):>7}{wins:>5}{len(items) - wins:>6}"
            f"{wins * 100 / len(items):>6.0f}%{money(net):>12}"
            f"{f'{average:.2f}' if average is not None else '—':>9}"
        )
    print(THIN)
    thin = [start for start, items in buckets.items() if len(items) < 5]
    if thin:
        print(f"  {len(thin)} of {len(buckets)} half-hours hold fewer than 5 trades. A win rate")
        print("  built on four trades includes both 'excellent' and 'ruinous' in its range,")
        print("  so do not move the start time on one of those rows.")
    print("  Move it only on a bucket that is both thick and consistently bad.")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="How far back to look. Ignored if --from is given.")
    parser.add_argument("--from", dest="from_date", type=parse_day, help="First session (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=parse_day, help="Last session. Default: today in IST.")
    parser.add_argument("--ledger", choices=("live", "paper"), default="live", help="Which account. Default live.")
    args = parser.parse_args()

    to_date = args.to_date or datetime.now(MARKET_TZ).date()
    from_date = args.from_date or (to_date - timedelta(days=max(args.days, 1)))

    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS)
        square_off = datetime.strptime(str(controls.session_square_off_time), "%H:%M").time()
        records = await load_trades(
            session, from_date, to_date, execution_mode=LIVE if args.ledger == "live" else PAPER
        )

    print(RULE)
    print(f"TRADE PATTERNS — {from_date} to {to_date}   ({args.ledger})")
    print(RULE)
    if not records:
        print("  No trade in this range.")
        print(RULE)
        return 0
    days = sorted({record.session_date for record in records})
    print(f"  {len(records)} trade(s) across {len(days)} session(s), {days[0]} to {days[-1]}.")
    print()

    by_stock(records)
    same_day_repeats(records)
    how_they_ended(records, square_off)
    by_entry_time(records)

    print(RULE)
    print("  Counts only. Nothing here was changed, and this script cannot place,")
    print("  cancel or modify an order.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
