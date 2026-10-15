"""Sweep a parameter grid over stored history, in sample and out of it.

The question this answers is the one five live trades cannot: does a lower
score threshold, or a different reward:risk, actually make more money over
months rather than over a week. It replays stored candles only -- no broker, no
orders, nothing live -- and splits the sessions into an in-sample part it tunes
on and a validation part it does not, because the best row of any sweep is also
the row most likely to be an accident.

``proven`` means the combination made money on the validation sessions with at
least three trades there. A row that is not proven is a row that looked good on
the half of the data it was chosen from, which is what overfitting is.

Memory, not compute, is the limit on a NAS: a candle costs about 680 bytes, so
six months across 29 instruments is roughly 0.9 GB before the replays start.
The run therefore estimates first and refuses a grid that will not fit, rather
than being killed halfway through.

Read-only. Safe during a session, but it is a long job on a small machine --
prefer after 15:30.

Usage:

    docker compose exec api python scripts/run_sweep.py --score 60,65,71,75
    docker compose exec api python scripts/run_sweep.py --score 60,65,71 --rr 1.5,2.0,2.5
    docker compose exec api python scripts/run_sweep.py --sessions 120 --score 65,71 \
        --instrument "NSE_EQ|INE423A01024" --instrument "NSE_EQ|INE160A01022"
    docker compose exec api python scripts/run_sweep.py --param min_stop_distance_percent=0.6,0.8,1.0
"""

import argparse
import asyncio
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import ApplicationSetting, MarketCandle  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.backtest_sweep import SWEEPABLE, build_combinations, run_parameter_sweep  # noqa: E402
from app.services.market_calculations import CompletedCandle  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 110
BYTES_PER_CANDLE = 680
# Rows fetched per round trip while loading. Large enough that the streaming
# costs no measurable time against a local database, small enough that the
# batch itself never matters beside the candles being kept.
CANDLE_BATCH = 5_000


def clock() -> str:
    """Wall-clock time on each progress line.

    Elapsed-and-remaining says how long a run should take; this says when each
    step actually happened, which is what tells a stalled run from a slow one
    when you come back to the log hours later.
    """
    return datetime.now(MARKET_TIMEZONE).strftime("%H:%M:%S")


def heading(text: str) -> None:
    print("\n" + RULE + f"\n{text}\n" + RULE)


def numbers(value: str) -> list[float]:
    out: list[float] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        number = float(part)
        out.append(int(number) if number.is_integer() else number)
    return out


# Every column ``completed`` reads, and no more. session_date is deliberately
# absent: CompletedCandle derives it from opened_at, and selecting it would be
# a second source for a value the dataclass already computes.
CANDLE_COLUMNS = (
    MarketCandle.instrument_token,
    MarketCandle.timeframe_seconds,
    MarketCandle.opened_at,
    MarketCandle.closed_at,
    MarketCandle.open,
    MarketCandle.high,
    MarketCandle.low,
    MarketCandle.close,
    MarketCandle.volume,
    MarketCandle.tick_count,
)


def completed(row) -> CompletedCandle:  # noqa: ANN001 - a MarketCandle or a Row of its columns
    return CompletedCandle(
        instrument_token=row.instrument_token,
        timeframe_seconds=row.timeframe_seconds,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        tick_count=row.tick_count,
    )


async def _ranked_only(
    by_instrument: dict[str, list[CompletedCandle]],
    app_settings,  # noqa: ANN001 - the Settings model
) -> dict[str, list[CompletedCandle]]:
    """Drop the candles the live scanner would never have seen.

    The replay has no universe gate, so a backtest trades every instrument it
    is handed -- including the low-ATR large caps the daily ranking refuses.
    On 13 October those were BANKBARODA, BHARTIARTL, ICICIBANK, RELIANCE and
    SBIN: exactly the shares where a 1.2% target sits outside the day's range,
    which is the failure the ATR band exists to prevent. A sweep that includes
    them is not measuring the system that runs.

    No engine change is needed for this. The replay skips any session an
    instrument has fewer than three candles for, so removing the candles for a
    session the ranking rejected is the same as gating it.

    The ranking is **recomputed** per session rather than read from
    ``scan_universe``, which only holds rows for the few days since the
    feature was switched on. It is recomputed with the real ``rank_universe``
    from daily candles collapsed out of the stored minutes, so the filter here
    is the filter that runs live rather than another implementation of it.
    """
    from app.services.backtesting import _session_daily_candle
    from app.services.universe import UniverseControls, _stored_controls, rank_universe

    controls = UniverseControls.from_settings(app_settings, await _stored_controls())

    # Per instrument: one daily candle per session, and the session's open.
    daily: dict[str, dict[date, CompletedCandle]] = {}
    opens: dict[str, dict[date, object]] = {}
    sessions: set[date] = set()
    for token, rows in by_instrument.items():
        by_date: dict[date, list[CompletedCandle]] = {}
        for candle in rows:
            by_date.setdefault(candle.session_date, []).append(candle)
        daily[token] = {day: _session_daily_candle(items) for day, items in by_date.items() if items}
        opens[token] = {
            day: sorted(items, key=lambda candle: candle.opened_at)[0].open for day, items in by_date.items() if items
        }
        sessions |= set(by_date)

    history = max(int(getattr(app_settings, "daily_history_sessions", 10) or 10), 2)
    ordered = sorted(sessions)
    selected_by_session: dict[date, set[str]] = {}
    for day in ordered:
        prior = [earlier for earlier in ordered if earlier < day][-history:]
        if not prior:
            # Nothing to rank on. The live gate fails open in this position, so
            # the backtest does too rather than silently trading nothing.
            selected_by_session[day] = {token for token in by_instrument if day in daily.get(token, {})}
            continue
        candidates = {
            token: [daily[token][earlier] for earlier in prior if earlier in daily[token]]
            for token in by_instrument
            if day in daily.get(token, {})
        }
        session_opens = {token: opens[token][day] for token in candidates if day in opens[token]}
        ranked = rank_universe(candidates, session_opens, controls)
        selected_by_session[day] = {item.instrument_token for item in ranked if item.selected}

    kept: dict[str, list[CompletedCandle]] = {}
    dropped = 0
    for token, rows in by_instrument.items():
        mine = [candle for candle in rows if token in selected_by_session.get(candle.session_date, set())]
        dropped += len(rows) - len(mine)
        if mine:
            kept[token] = mine

    per_session = [len(value) for value in selected_by_session.values()]
    average = sum(per_session) / len(per_session) if per_session else 0
    print()
    print("  RANKED ONLY — replaying what the live scanner would have scanned")
    print(f"    ranking recomputed for {len(selected_by_session)} session(s)")
    print(
        f"    instruments selected : {min(per_session, default=0)}-{max(per_session, default=0)}"
        f" per session, {average:.1f} on average of {len(by_instrument)}"
    )
    print(f"    candles dropped      : {dropped:,}")
    print(f"    instruments remaining: {len(kept)}")
    if not kept:
        print("    Nothing survived the filter. Check universe_min_atr_percent -- at this setting")
        print("    the ranking refuses everything, which is a finding about the setting.")
    return kept


async def main() -> int:
    # Line-buffered, because this runs for hours with its output redirected to
    # a file and Python block-buffers a pipe. A run that was killed therefore
    # lost whatever had not filled an 8 KB buffer, and the log's last line was
    # the last flush rather than the last thing that happened -- so "it died at
    # [1/4]" could not be told apart from "it died two hours later". A
    # long-running job whose log goes quiet for hours has to be observable
    # before it can be diagnosed.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", type=numbers, help="minimum_score values, e.g. 60,65,71,75")
    parser.add_argument("--rr", type=numbers, help="minimum_rr values, e.g. 1.5,2.0,2.5")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Any other sweepable parameter as key=v1,v2. Repeatable.",
    )
    parser.add_argument("--sessions", type=int, default=60, help="How many of the most recent stored sessions.")
    parser.add_argument("--instrument", action="append", dest="instruments", help="Limit instruments; repeatable.")
    parser.add_argument("--strategy", help="Strategy id to sweep. Default: the first enabled one.")
    parser.add_argument("--timeframe", type=int, default=60)
    parser.add_argument("--validation-fraction", type=float, default=0.35)
    parser.add_argument(
        "--ranked-only",
        action="store_true",
        help=(
            "Replay only the instruments the daily ranking would have selected on each session, "
            "which is what the live scanner trades. Off, every instrument is replayed, including "
            "the ones the ATR band refuses."
        ),
    )
    parser.add_argument("--memory-budget-mb", type=int, default=700, help="Refuse a run estimated above this.")
    parser.add_argument("--force", action="store_true", help="Run anyway. On a small machine this may be killed.")
    args = parser.parse_args()

    grid: dict[str, list] = {}
    if args.score:
        grid["minimum_score"] = args.score
    if args.rr:
        grid["minimum_rr"] = args.rr
    for item in args.param:
        if "=" not in item:
            print(f"--param needs key=v1,v2 form; got {item!r}")
            return 1
        key, _, values = item.partition("=")
        grid[key.strip()] = numbers(values)
    if not grid:
        print("Nothing to sweep. Give at least --score, --rr or --param.")
        return 1
    unknown = sorted(set(grid) - SWEEPABLE)
    if unknown:
        print(f"Not sweepable: {', '.join(unknown)}")
        print(f"Sweepable parameters are: {', '.join(sorted(SWEEPABLE))}")
        return 1

    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
    from app.core.config import get_settings
    from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PaperExecutionControls
    from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration

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

        parsed = [StrategyConfiguration.model_validate(item) for item in stored]
        if args.strategy:
            chosen = next((item for item in parsed if item.id == args.strategy), None)
        else:
            chosen = next((item for item in parsed if item.enabled), None)
        if chosen is None:
            print("No strategy to sweep. Enable one, or pass --strategy with an id.")
            print(f"  known: {', '.join(item.id for item in parsed)}")
            return 1

        # The most recent N stored sessions, which is what "six months" means
        # once holidays and gaps are taken out.
        days = [
            row
            for row in (
                await session.scalars(
                    select(MarketCandle.session_date)
                    .where(MarketCandle.timeframe_seconds == args.timeframe)
                    .distinct()
                    .order_by(MarketCandle.session_date.desc())
                    .limit(args.sessions)
                )
            ).all()
        ]
        if not days:
            print("No stored candles at that timeframe. Run scripts/backfill_history.py first.")
            return 1
        start, end = min(days), max(days)

        tokens = args.instruments or [
            row
            for row in (
                await session.scalars(
                    select(MarketCandle.instrument_token)
                    .where(
                        MarketCandle.timeframe_seconds == args.timeframe,
                        MarketCandle.session_date >= start,
                    )
                    .distinct()
                )
            ).all()
        ]
        tradeable = [token for token in tokens if token not in benchmark_keys]
        wanted = list(dict.fromkeys([*tradeable, *benchmark_keys]))

        total = await session.scalar(
            select(func.count(MarketCandle.id)).where(
                MarketCandle.instrument_token.in_(wanted),
                MarketCandle.timeframe_seconds == args.timeframe,
                MarketCandle.session_date >= start,
                MarketCandle.session_date <= end,
            )
        )

    combinations = build_combinations(grid, app_settings.backtest_sweep_max_combinations)
    estimate_mb = (total or 0) * BYTES_PER_CANDLE / 1024 / 1024

    heading(f"SWEEP — {chosen.name} ({chosen.id})")
    print(f"  sessions            : {len(days)}   {start} -> {end}")
    print(f"  instruments         : {len(tradeable)} tradeable, plus the benchmark")
    print(f"  candles to load     : {total:,}   about {estimate_mb:,.0f} MB in memory")
    print(f"  grid                : {grid}")
    print(f"  combinations        : {len(combinations)}")
    print(f"  validation fraction : {args.validation_fraction}")

    if estimate_mb > args.memory_budget_mb and not args.force:
        print()
        print(f"  Refused: that is over the {args.memory_budget_mb} MB budget and this machine would")
        print("  likely kill the process halfway through, after an hour of work. Narrow it:")
        print("    --sessions 40                 fewer sessions")
        print("    --instrument <key>            repeatable; the names that actually signal")
        print("    --memory-budget-mb <n>        if you know this machine can take it")
        print("  Or --force to go ahead anyway.")
        return 1

    print("\n  Loading candles...")
    by_instrument: dict[str, list[CompletedCandle]] = {token: [] for token in tradeable}
    benchmark_series: dict[str, list[CompletedCandle]] = {key: [] for key in benchmark_keys}
    async with SessionLocal() as session:
        # Columns rather than entities, streamed in batches, and converted one
        # row at a time. Selecting MarketCandle itself built a mapped instance
        # per row and held every one of them -- in a list and in the session's
        # identity map -- while this loop built the CompletedCandle list beside
        # it. On 630,432 candles that is roughly a gigabyte of objects created
        # and immediately discarded, on top of the 409 MB the replay actually
        # needs. The estimate above counts only the latter, so a run passed the
        # 700 MB budget and was then killed by the kernel inside the first
        # replay: precisely the outcome the budget exists to prevent.
        #
        # A Row answers to the same attribute names, so ``completed`` is
        # unchanged and still the single place the mapping lives.
        stream = await session.stream(
            select(*CANDLE_COLUMNS)
            .where(
                MarketCandle.instrument_token.in_(wanted),
                MarketCandle.timeframe_seconds == args.timeframe,
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
            elif row.instrument_token in benchmark_series:
                benchmark_series[row.instrument_token].append(candle)
    benchmark = max(benchmark_series.values(), key=len, default=[])
    if not benchmark:
        print("  No benchmark (NIFTY) candles in this range; market-confirmation scoring will be degraded.")

    if args.ranked_only:
        by_instrument = await _ranked_only(by_instrument, app_settings)

    from app.services.backtest_sweep import STRATEGY_PARAMS

    shared = len(combinations) > 1 and set(grid) <= STRATEGY_PARAMS
    print(f"  Replaying {len(combinations)} combination(s) over {len(by_instrument)} instrument(s)...")
    if shared:
        print("  One shared pass: this grid only varies what the strategy carries, so every")
        print("  combination is scored against the same candles and the same indicators, and")
        print(f"  the whole sweep costs about what a single replay costs rather than {len(combinations)}.")
    else:
        touched = sorted(set(grid) - STRATEGY_PARAMS)
        print(f"  One pass per combination: {', '.join(touched)} change the controls or the")
        print("  indicator periods, which a replay takes once for the whole run. Expect this")
        print(f"  to cost about {len(combinations)} replays.")
    print()

    started = time.monotonic()
    first_finished: list[float] = []

    def report(finished: int, total: int, parameters: dict) -> None:
        """Say where we are, and after the first one say how long the rest will take.

        The estimate comes from this machine doing this work, because a replay
        rate guessed from anywhere else is worthless: the same grid is minutes
        on a laptop and most of a night on a NAS.
        """
        label = ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        elapsed = time.monotonic() - started
        if finished == 0:
            print(f"  [{finished + 1}/{total}] {label}")
            return
        if not first_finished:
            first_finished.append(elapsed)
        each = elapsed / finished
        remaining = each * (total - finished)
        print(
            f"  [{finished + 1}/{total}] {clock()}  {label}   "
            f"({each / 60:.1f} min each, about {remaining / 60:.0f} min left)"
        )

    result = run_parameter_sweep(
        chosen,
        controls.model_dump(),
        execution,
        app_settings,
        by_instrument,
        benchmark,
        grid,
        validation_fraction=args.validation_fraction,
        maximum_combinations=app_settings.backtest_sweep_max_combinations,
        progress=report,
    )
    took = time.monotonic() - started
    # Per *replayed* candle, not per loaded one. --ranked-only drops the
    # candles the live scanner would never have seen -- 40,188 of 52,188 on a
    # five-session run -- and dividing by the loaded count reported a machine
    # four times faster than it is. Anyone sizing a longer run from that figure
    # would underestimate it several-fold, which is the mistake that turned an
    # "hour of compute" into six.
    replayed = sum(len(rows) for rows in by_instrument.values())
    per_second = replayed * len(combinations) / max(took, 1)
    print(f"\n  Replays finished in {took / 60:.1f} min ({per_second:,.0f} candles/second).")
    if replayed != total:
        print(f"  {replayed:,} of {total:,} candles replayed per combination; the rest the ranking dropped.")

    heading("RESULTS — ranked by validation return, which is the half it was not tuned on")
    print(
        f"  {'#':>3} {'parameters':<34}"
        f"{'IN trades':>10}{'IN win%':>9}{'IN net':>10}"
        f"{'OUT trades':>11}{'OUT win%':>10}{'OUT net':>10}{'OUT PF':>8}  proven"
    )
    ordered = sorted(
        result.combinations,
        key=lambda item: (item.validation["return_percent"], item.validation["net_pnl"]),
        reverse=True,
    )
    for item in ordered:
        label = ", ".join(f"{key}={value}" for key, value in sorted(item.parameters.items()))
        factor = item.validation["profit_factor"]
        print(
            f"  {item.index:>3} {label[:33]:<34}"
            f"{item.in_sample['trades']:>10}{item.in_sample['win_rate_percent']:>9.1f}"
            f"{item.in_sample['net_pnl']:>10,.0f}"
            f"{item.validation['trades']:>11}{item.validation['win_rate_percent']:>10.1f}"
            f"{item.validation['net_pnl']:>10,.0f}"
            f"{(f'{factor:.2f}' if factor else '—'):>8}  {'yes' if item.proven else 'no'}"
        )

    heading("WHAT TO MAKE OF IT")
    proven = [item for item in ordered if item.proven]
    if not proven:
        print("  Nothing is proven: no combination made money on the validation sessions with at")
        print("  least three trades there. That is a real answer, not a failed run -- it says this")
        print("  grid contains no setting worth changing on this evidence. Do not pick the best")
        print("  in-sample row; that row is the one the tuning chose, and it is the most")
        print("  overfitted thing on the page.")
        return 0

    best = proven[0]
    label = ", ".join(f"{key}={value}" for key, value in sorted(best.parameters.items()))
    print(f"  Best proven combination: {label}")
    print(
        f"    in sample   {best.in_sample['trades']:>4} trades  {best.in_sample['win_rate_percent']:>5.1f}% win  "
        f"net {best.in_sample['net_pnl']:,.2f}"
    )
    print(
        f"    validation  {best.validation['trades']:>4} trades  {best.validation['win_rate_percent']:>5.1f}% win  "
        f"net {best.validation['net_pnl']:,.2f}"
    )
    if best.validation["trades"] < 20:
        print()
        print(f"  Only {best.validation['trades']} validation trades. That is suggestive, not settled: a win")
        print("  rate from a handful of trades has a confidence interval wide enough to include")
        print("  both 'worth changing' and 'worse than what you have'. Treat it as a reason to")
        print("  paper-forward the setting, not as a result.")
    print()
    print("  Charges here are modelled, not the broker's. A grid whose edge is thinner than the")
    print("  difference between the two is not telling you anything about your account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
