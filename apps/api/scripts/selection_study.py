"""Which signals should the day's three trades go to? A replay of selection rules.

The live account takes the first three signals that pass the ceilings. On 20
November all three filled at 09:47 and the day's risk was spent two minutes
into the window; a CPR breakout at 11:59 that reached its target could not have
been taken. 51 of the first 52 live trades entered before 10:00. This asks
whether a different *selection* rule, decided in advance, would have done
better over months of stored candles:

    now          first come first served, one trade per sector (what runs live)
    score        the same, but signals in the same minute go highest score first
    gap15/gap30  at most one new entry every 15 / 30 minutes
    two-early    at most two entries before 10:15, the third only after
    leader       trade only with the day's leading sector (LONG in the two
                 strongest against Nifty, SHORT in the two weakest), two a sector
    leader+gap15 both
    pooled-*     the same rules over signals from every strategy, not just RS

It replays every strategy once over the stored candles to get each share's
candidate trade per session (with its score and its result after charges),
then runs each rule over those candidates day by day with the live account's
ceilings: three trades, one per share, the sector cap, the daily loss limit.
Sessions are split in time; a rule is only worth anything if it also wins on
the later part it was not chosen on.

Read-only: selects candles and settings, writes nothing, places nothing. Heavy
-- run it after 15:30 with nohup and a log file, and under `timeout` so it can
never run into a session.

    nohup timeout 4h python scripts/selection_study.py --sessions 125 > /tmp/selection.log 2>&1 &
"""

import argparse
import asyncio
import gc
import json
import sys
import time
from array import array
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import ApplicationSetting, MarketCandle  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.market_calculations import CompletedCandle  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402
from run_sweep import CANDLE_BATCH, CANDLE_COLUMNS, _ranked_only, completed  # noqa: E402

RULE = "=" * 104
MAIN_STRATEGY = "rs-pullback-default"


@dataclass
class Candidate:
    strategy: str
    token: str
    symbol: str
    sector: str
    day: str
    side: str
    entered: datetime  # IST
    exited: datetime  # IST
    score: int
    net: float
    r: float
    exit_reason: str
    sector_rank: int | None = None  # 1 = strongest against Nifty at the signal
    sector_count: int = 0
    sector_vs_nifty: float = 0.0


def clock() -> str:
    return datetime.now(MARKET_TIMEZONE).strftime("%H:%M:%S")


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def minute_of(moment: datetime) -> int:
    local = moment.astimezone(MARKET_TIMEZONE)
    return (local.hour * 60 + local.minute) - (9 * 60 + 15)


def pct_series(rows: list[CompletedCandle]) -> array:
    """Percent change from the session open, by minute since 09:15, carried forward."""
    out = array("f", [float("nan")] * 375)
    if not rows:
        return out
    day_open = float(rows[0].open) or 1.0
    for candle in rows:
        index = minute_of(candle.opened_at)
        if 0 <= index < 375:
            out[index] = (float(candle.close) / day_open - 1.0) * 100.0
    last = 0.0
    for index in range(375):
        if out[index] != out[index]:  # NaN
            out[index] = last
        else:
            last = out[index]
    return out


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def load(tokens: list[str], benchmark_keys: set[str], start, end, timeframe: int):
    by_instrument: dict[str, list[CompletedCandle]] = {token: [] for token in tokens}
    bench: dict[str, list[CompletedCandle]] = {key: [] for key in benchmark_keys}
    async with SessionLocal() as session:
        stream = await session.stream(
            select(*CANDLE_COLUMNS)
            .where(
                MarketCandle.instrument_token.in_([*tokens, *benchmark_keys]),
                MarketCandle.timeframe_seconds == timeframe,
                MarketCandle.session_date >= start,
                MarketCandle.session_date <= end,
            )
            .order_by(MarketCandle.instrument_token, MarketCandle.opened_at)
            .execution_options(yield_per=CANDLE_BATCH)
        )
        async for row in stream:
            candle = completed(row)
            if row.instrument_token in by_instrument:
                by_instrument[row.instrument_token].append(candle)
            elif row.instrument_token in bench:
                bench[row.instrument_token].append(candle)
    return by_instrument, max(bench.values(), key=len, default=[])


def by_day(rows: list[CompletedCandle]) -> dict[str, list[CompletedCandle]]:
    out: dict[str, list[CompletedCandle]] = defaultdict(list)
    for candle in rows:
        out[candle.session_date.isoformat()].append(candle)
    return out


async def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessions", type=int, default=125)
    parser.add_argument("--parts", type=int, default=3, help="Replay the instruments in this many groups (memory).")
    parser.add_argument("--validation-fraction", type=float, default=0.35)
    parser.add_argument("--timeframe", type=int, default=60)
    parser.add_argument("--no-ranked-only", action="store_true", help="Replay every instrument, not only ranked ones.")
    parser.add_argument("--save", default="/tmp/selection_candidates.json", help="Write the candidates here.")
    parser.add_argument("--from-file", help="Skip the replay and read candidates saved by an earlier run.")
    args = parser.parse_args()

    if args.from_file:
        payload = json.loads(Path(args.from_file).read_text())
        candidates = [
            Candidate(
                **{
                    **item,
                    "entered": datetime.fromisoformat(item["entered"]),
                    "exited": datetime.fromisoformat(item["exited"]),
                }
            )
            for item in payload["candidates"]
        ]
        days = payload["days"]
        report(candidates, days, args.validation_fraction, payload["controls"])
        return 0

    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
    from app.core.config import get_settings
    from app.services.backtesting import run_completed_candle_backtest
    from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PaperExecutionControls
    from app.services.sectors import sector_of
    from app.services.strategy_registry import (
        DEFAULT_STRATEGIES,
        STRATEGIES_KEY,
        StrategyConfiguration,
        StrategyRegistry,
    )
    from app.services.trading_symbols import resolve_script_names

    app_settings = get_settings()
    benchmark_keys = {app_settings.nifty_benchmark_token, app_settings.upstox_nifty_benchmark_key}

    async with SessionLocal() as session:
        controls_row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(controls_row.value if controls_row else DEFAULT_TRADING_CONTROLS)
        strategies_row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        stored = strategies_row.value if strategies_row else DEFAULT_STRATEGIES
        execution_row = await session.get(ApplicationSetting, "paper_execution_controls")
        execution = PaperExecutionControls.model_validate(
            execution_row.value if execution_row else DEFAULT_PAPER_EXECUTION_CONTROLS
        )
        strategies = [
            StrategyConfiguration.model_validate(item).model_copy(update={"enabled": True}) for item in stored
        ]
        days = sorted(
            d.isoformat()
            for d in (
                await session.scalars(
                    select(MarketCandle.session_date)
                    .where(MarketCandle.timeframe_seconds == args.timeframe)
                    .distinct()
                    .order_by(MarketCandle.session_date.desc())
                    .limit(args.sessions)
                )
            ).all()
        )
        latest = max(days)
        # The watchlist that runs now: whatever stored candles on the last session.
        tokens = sorted(
            t
            for t in (
                await session.scalars(
                    select(MarketCandle.instrument_token)
                    .where(
                        MarketCandle.timeframe_seconds == args.timeframe,
                        MarketCandle.session_date == datetime.fromisoformat(latest).date(),
                    )
                    .distinct()
                )
            ).all()
            if t not in benchmark_keys
        )
        names = await resolve_script_names(session, tokens)

    start = datetime.fromisoformat(days[0]).date()
    end = datetime.fromisoformat(days[-1]).date()
    sectors = {token: sector_of(names.get(token, token)) for token in tokens}
    controls_dump = controls.model_dump()

    heading("SELECTION STUDY — replay")
    print(f"  sessions     : {len(days)}   {start} -> {end}")
    print(f"  instruments  : {len(tokens)} (the watchlist stored on {latest}), in {args.parts} part(s)")
    print(f"  strategies   : {', '.join(s.id for s in strategies)}")
    print(f"  ranked only  : {not args.no_ranked_only}")

    # Scores are not on the trade result; record them as the replay asks.
    scores: dict[tuple[str, str, datetime], int] = {}
    original = StrategyRegistry.evaluate

    def recording(configuration, candle, indicators, benchmark, controls_, prior_state):  # noqa: ANN001, ANN202
        decision = original(configuration, candle, indicators, benchmark, controls_, prior_state)
        if decision.side is not None:
            scores[(configuration.id, candle.instrument_token, candle.closed_at)] = int(decision.score or 0)
        return decision

    StrategyRegistry.evaluate = staticmethod(recording)

    pct: dict[str, dict[str, array]] = defaultdict(dict)  # day -> token -> series
    nifty_pct: dict[str, array] = {}
    raw: list[tuple] = []
    parts = [tokens[i :: args.parts] for i in range(args.parts)]
    started = time.monotonic()
    for number, part in enumerate(parts, 1):
        print(f"\n  [{number}/{len(parts)}] {clock()} loading {len(part)} instrument(s)...")
        by_instrument, benchmark = await load(part, benchmark_keys, start, end, args.timeframe)
        for token, rows in by_instrument.items():
            for day, day_rows in by_day(rows).items():
                pct[day][token] = pct_series(day_rows)
        if not nifty_pct:
            for day, day_rows in by_day(benchmark).items():
                nifty_pct[day] = pct_series(day_rows)
        if not args.no_ranked_only:
            by_instrument = await _ranked_only(by_instrument, app_settings)
        count = sum(len(v) for v in by_instrument.values())
        print(f"  [{number}/{len(parts)}] {clock()} replaying {count:,} candles across {len(strategies)} strategies...")
        result = run_completed_candle_backtest(
            by_instrument, benchmark, strategies, controls_dump, execution, app_settings
        )
        for trade in result.trades:
            raw.append(
                (
                    trade.strategy_id,
                    trade.instrument_token,
                    trade.session_date,
                    trade.side,
                    trade.signal_at,
                    trade.entered_at,
                    trade.exited_at,
                    float(trade.net_pnl),
                    float(trade.realized_r),
                    trade.exit_reason,
                )
            )
        print(
            f"  [{number}/{len(parts)}] {clock()} done: {len(result.trades)} candidate trade(s), "
            f"{(time.monotonic() - started) / 60:.1f} min so far"
        )
        del by_instrument, benchmark, result
        gc.collect()
    StrategyRegistry.evaluate = original

    candidates: list[Candidate] = []
    for strategy_id, token, day, side, signal_at, entered, exited, net, r, reason in raw:
        candidates.append(
            Candidate(
                strategy=strategy_id,
                token=token,
                symbol=names.get(token, token),
                sector=sectors.get(token, token),
                day=day,
                side=side,
                entered=entered.astimezone(MARKET_TIMEZONE),
                exited=exited.astimezone(MARKET_TIMEZONE),
                score=scores.get((strategy_id, token, signal_at), 0),
                net=net,
                r=r,
                exit_reason=reason,
            )
        )
    rank_sectors(candidates, pct, nifty_pct, sectors)

    Path(args.save).write_text(
        json.dumps(
            {
                "days": days,
                "controls": {k: str(v) for k, v in controls_dump.items()},
                "candidates": [
                    {**c.__dict__, "entered": c.entered.isoformat(), "exited": c.exited.isoformat()}
                    for c in candidates
                ],
            },
            default=str,
        )
    )
    print(f"\n  {len(candidates)} candidates saved to {args.save} (re-report with --from-file).")
    report(candidates, days, args.validation_fraction, {k: str(v) for k, v in controls_dump.items()})
    return 0


def rank_sectors(candidates, pct, nifty_pct, sectors) -> None:  # noqa: ANN001
    """Each candidate's sector rank at its signal minute, by mean move from the open."""
    for c in candidates:
        minute = minute_of(c.entered) - 1  # the signal candle is the one before entry
        series = pct.get(c.day, {})
        if minute < 0 or not series:
            continue
        groups: dict[str, list[float]] = defaultdict(list)
        for token, values in series.items():
            groups[sectors.get(token, token)].append(values[min(minute, 374)])
        strength = {group: sum(v) / len(v) for group, v in groups.items()}
        ordered = sorted(strength, key=lambda g: -strength[g])
        if c.sector in strength:
            c.sector_rank = ordered.index(c.sector) + 1
            c.sector_count = len(ordered)
            nifty = nifty_pct.get(c.day)
            c.sector_vs_nifty = strength[c.sector] - (nifty[min(minute, 374)] if nifty else 0.0)


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------


def aligned_with_leader(c: Candidate, top: int = 2) -> bool:
    if c.sector_rank is None:
        return False
    if c.side == "LONG":
        return c.sector_rank <= top and c.sector_vs_nifty > 0
    return c.sector_rank > c.sector_count - top and c.sector_vs_nifty < 0


def select_day(
    pool: list[Candidate],
    *,
    max_trades: int,
    loss_limit: float,
    per_sector: int,
    by_score: bool,
    gap_minutes: int = 0,
    early_cap: int | None = None,
    early_until: str = "10:15",
    leader: bool = False,
) -> list[Candidate]:
    if leader:
        pool = [c for c in pool if aligned_with_leader(c)]
    if by_score:
        pool = sorted(pool, key=lambda c: (c.entered, -c.score, c.token))
    else:
        pool = sorted(pool, key=lambda c: (c.entered, c.token))
    taken: list[Candidate] = []
    for c in pool:
        if len(taken) >= max_trades:
            break
        if any(t.token == c.token for t in taken):
            continue
        if sum(1 for t in taken if t.sector == c.sector) >= per_sector:
            continue
        realised = sum(t.net for t in taken if t.exited <= c.entered)
        if realised <= -loss_limit:
            break
        if gap_minutes and taken and c.entered - taken[-1].entered < timedelta(minutes=gap_minutes):
            continue
        if early_cap is not None and c.entered.strftime("%H:%M") < early_until:
            if sum(1 for t in taken if t.entered.strftime("%H:%M") < early_until) >= early_cap:
                continue
        taken.append(c)
    return taken


def policies(controls: dict) -> dict[str, tuple[bool, dict]]:
    n = int(float(controls.get("maximum_daily_trades", 3)))
    loss = float(controls.get("daily_loss_limit", 750))
    sector = int(float(controls.get("maximum_daily_trades_per_sector", 1)))
    base = {"max_trades": n, "loss_limit": loss, "per_sector": sector}
    # name -> (pooled across strategies?, rule)
    return {
        "now": (False, {**base, "by_score": False}),
        "score": (False, {**base, "by_score": True}),
        "gap15": (False, {**base, "by_score": True, "gap_minutes": 15}),
        "gap30": (False, {**base, "by_score": True, "gap_minutes": 30}),
        "two-early": (False, {**base, "by_score": True, "early_cap": 2}),
        "leader": (False, {**base, "by_score": True, "leader": True, "per_sector": 2}),
        "leader+gap15": (False, {**base, "by_score": True, "leader": True, "per_sector": 2, "gap_minutes": 15}),
        "pooled-now": (True, {**base, "by_score": False}),
        "pooled-gap15": (True, {**base, "by_score": True, "gap_minutes": 15}),
        "pooled-leader+gap15": (
            True,
            {**base, "by_score": True, "leader": True, "per_sector": 2, "gap_minutes": 15},
        ),
    }


def stats(trades: list[Candidate]) -> tuple[int, float, float, float, float]:
    if not trades:
        return 0, 0.0, 0.0, 0.0, 0.0
    won = sum(1 for t in trades if t.net > 0)
    net = sum(t.net for t in trades)
    gains = sum(t.net for t in trades if t.net > 0)
    losses = -sum(t.net for t in trades if t.net < 0)
    pf = gains / losses if losses else float("inf")
    return len(trades), won / len(trades) * 100, net, net / len(trades), pf


def report(candidates: list[Candidate], days: list[str], validation_fraction: float, controls: dict) -> None:
    cut = days[int(len(days) * (1 - validation_fraction))]
    by_day_all: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_day_all[c.day].append(c)

    heading("1. EVERY CANDIDATE, BEFORE ANY SELECTION — by strategy (after charges)")
    print(f"  in-sample {days[0]} .. before {cut}   out-of-sample {cut} .. {days[-1]}")
    print(f"  {'strategy':<24}{'IN n':>7}{'win%':>7}{'net':>11}{'/trade':>8}{'OUT n':>8}{'win%':>7}{'net':>11}{'/trade':>8}")
    for sid in sorted({c.strategy for c in candidates}):
        mine = [c for c in candidates if c.strategy == sid]
        a = stats([c for c in mine if c.day < cut])
        b = stats([c for c in mine if c.day >= cut])
        print(f"  {sid:<24}{a[0]:>7}{a[1]:>6.1f}%{a[2]:>11,.0f}{a[3]:>8.0f}{b[0]:>8}{b[1]:>6.1f}%{b[2]:>11,.0f}{b[3]:>8.0f}")

    heading(f"2. {MAIN_STRATEGY} CANDIDATES BY ENTRY TIME — are later signals better?")
    buckets: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        if c.strategy != MAIN_STRATEGY:
            continue
        hh, mm = c.entered.hour, c.entered.minute
        start = f"{hh:02d}:{0 if mm < 30 else 30:02d}"
        buckets[start].append(c)
    print(f"  {'entered from':<14}{'n':>6}{'win%':>7}{'net':>11}{'/trade':>8}{'avg R':>8}")
    for key in sorted(buckets):
        n, win, net, per, _ = stats(buckets[key])
        avg_r = sum(t.r for t in buckets[key]) / n
        print(f"  {key:<14}{n:>6}{win:>6.1f}%{net:>11,.0f}{per:>8.0f}{avg_r:>8.2f}")

    heading(f"3. {MAIN_STRATEGY} CANDIDATES BY SECTOR ALIGNMENT")
    aligned = [c for c in candidates if c.strategy == MAIN_STRATEGY and aligned_with_leader(c)]
    against = [c for c in candidates if c.strategy == MAIN_STRATEGY and not aligned_with_leader(c)]
    for label, group in (("with the leading sector", aligned), ("everything else", against)):
        n, win, net, per, pf = stats(group)
        print(f"  {label:<26}{n:>6}  win {win:5.1f}%  net {net:>10,.0f}  /trade {per:>6.0f}  PF {pf:.2f}")

    heading("4. SELECTION RULES, DAY BY DAY WITH THE LIVE CEILINGS — the answer")
    print(f"  {'rule':<22}{'IN n':>6}{'win%':>7}{'net':>10}{'/tr':>6}{'PF':>6}{'OUT n':>7}{'win%':>7}{'net':>10}{'/tr':>6}{'PF':>6}")
    rows = []
    for name, (pooled, rule) in policies(controls).items():
        chosen: list[Candidate] = []
        for day in days:
            pool = by_day_all.get(day, [])
            if not pooled:
                pool = [c for c in pool if c.strategy == MAIN_STRATEGY]
            chosen.extend(select_day(pool, **rule))
        a = stats([c for c in chosen if c.day < cut])
        b = stats([c for c in chosen if c.day >= cut])
        rows.append((name, a, b))
        print(
            f"  {name:<22}{a[0]:>6}{a[1]:>6.1f}%{a[2]:>10,.0f}{a[3]:>6.0f}{a[4]:>6.2f}"
            f"{b[0]:>7}{b[1]:>6.1f}%{b[2]:>10,.0f}{b[3]:>6.0f}{b[4]:>6.2f}"
        )
    print()
    print("  A rule is worth taking live only if it beats 'now' on BOTH halves, and the")
    print("  out-of-sample half has enough trades to mean something. Net is after the")
    print("  replay's charge model; the replay fills at the next candle's open.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
