"""Read-only: backtest the swing strategies on the stored daily history.

Writes nothing. Reads about 200 shares x 2 years of daily candles (roughly
100,000 rows) and replays them in memory, so it waits for 3:35 PM IST on a
trading weekday rather than compete with the intraday scanner.

    docker compose exec -T api python scripts/run_swing_backtest.py
    docker compose exec -T api python scripts/run_swing_backtest.py --strategy swing-trend-pullback-v1
    docker compose exec -T api python scripts/run_swing_backtest.py --trades

Results are split: the first 70% of the history (in-sample) and the last 30%
(out-of-sample). The rules were fixed before any run, so the out-of-sample
half was never used to choose them; it is the half that means anything. Every
figure is after delivery charges, and leans optimistic: the universe is
today's NIFTY 200, so shares that left the index over the period are missing.
"""

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.market_calculations import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78


def market_hours_now(now: datetime) -> bool:
    from datetime import time

    return now.weekday() < 5 and time(9, 0) <= now.time() <= time(15, 35)


def show(label: str, summary) -> None:  # noqa: ANN001
    pf = f"{summary.profit_factor:.2f}" if summary.profit_factor is not None else "-"
    print(
        f"  {label:<16}{summary.trades:>7}{summary.win_rate * 100:>8.1f}%{summary.average_r:>9}"
        f"{summary.expectancy:>12}{summary.net_pnl:>13}{summary.charges:>11}{pf:>7}{summary.max_drawdown:>11}"
        f"{summary.average_holding_days:>7.1f}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--strategy", action="append", dest="strategies")
    parser.add_argument("--trades", action="store_true", help="List every out-of-sample trade.")
    args = parser.parse_args()

    now = datetime.now(MARKET_TIMEZONE)
    if market_hours_now(now):
        print(f"Waiting for the close: it is {now:%I:%M %p} IST on a trading weekday. Run after 3:35 PM IST.")
        return 2

    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.services.charges import swing_charges
    from app.services.swing_backtest import SwingRules, metrics, run_backtest, split
    from app.services.swing_data import load_adjusted_daily
    from app.services.swing_strategies import STRATEGIES
    from app.services.swing_universe import nifty200

    settings = get_settings()
    to_date = now.date()
    from_date = to_date - timedelta(days=365 * max(args.years, 1))
    members = nifty200()
    rules = SwingRules(strategies=tuple(args.strategies or STRATEGIES))

    async with SessionLocal() as session:
        # Settings > Charges, the same card the swing paper desk uses.
        card = await swing_charges(session)
        benchmark = await load_adjusted_daily(session, settings.upstox_nifty_benchmark_key, from_date=from_date)
        universe = {
            member.instrument_token: await load_adjusted_daily(session, member.instrument_token, from_date=from_date)
            for member in members
        }
    if len(benchmark) < 250:
        print(f"Only {len(benchmark)} NIFTY daily candles are stored; the strategies need 200+ of warm-up.")
        print("Run scripts/swing_backfill.py first.")
        return 1
    loaded = sum(1 for bars in universe.values() if bars)
    sectors = {member.instrument_token: member.industry for member in members}
    symbols = {member.instrument_token: member.symbol for member in members}

    result = run_backtest(universe, sectors, benchmark, rules=rules, costs=card.cost_model())
    cut, inside, outside = split(result, 0.7)

    print(RULE + f"\nSWING BACKTEST {benchmark[0].session_date} -> {benchmark[-1].session_date}\n" + RULE)
    print(f"  shares with data : {loaded} of {len(members)}")
    print(
        f"  paper capital    : Rs {rules.capital:,}  risk {rules.risk_per_trade_percent}% = Rs {rules.risk_per_trade:,} a trade"
    )
    print(
        f"  limits           : {rules.max_open_positions} open, {rules.max_per_sector} per sector, heat {rules.heat_cap_percent}%"
    )
    print(f"  strategies       : {', '.join(rules.strategies)}")
    print(
        f"  charges          : brokerage Rs {card.brokerage_per_order:g}/order, DP Rs {card.dp_charge_per_sale:g}/sale,"
        f" STT {card.stt_percent:g}% each side (Settings > Charges)"
    )
    print(f"  out-of-sample    : entries from {cut} on")
    print()
    print(
        f"  {'':<16}{'trades':>7}{'win':>9}{'avg R':>9}{'Rs/trade':>12}{'net Rs':>13}{'charges':>11}{'PF':>7}{'max DD':>11}{'days':>7}"
    )
    show("in-sample", metrics(inside))
    show("OUT-OF-SAMPLE", metrics(outside))
    for name in rules.strategies:
        print(f"\n  {name}")
        show("  in-sample", metrics([t for t in inside if t.setup.strategy == name]))
        show("  out-of-sample", metrics([t for t in outside if t.setup.strategy == name]))

    print(
        "\n  exits (all)    : "
        + ", ".join(f"{k} {v}" for k, v in Counter(t.exit_reason for t in result.trades).most_common())
    )
    print(
        "  not taken      : " + ", ".join(f"{k} {v}" for k, v in sorted(result.skipped.items(), key=lambda kv: -kv[1]))
    )

    if args.trades:
        print(RULE + "\nOUT-OF-SAMPLE TRADES\n" + RULE)
        for t in sorted(outside, key=lambda trade: trade.entry_date):
            print(
                f"  {t.entry_date} {symbols.get(t.token, t.token):<12} {t.setup.strategy:<24} qty {t.quantity:>4}"
                f" in {t.entry_price:>9} out {t.exit_date} {t.exit_reason:<14} net {t.net_pnl:>9.2f}  R {t.r_multiple:>5.2f}"
            )

    print("\n  Read the OUT-OF-SAMPLE line, not the in-sample one. Figures are after charges and lean optimistic")
    print("  (today's index members only). A small trade count is an anecdote, not evidence. Wrote nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
