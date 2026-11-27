"""Replay a long period over many shares a month at a time, in bounded memory.

``run_completed_candle_backtest`` replays each share on its own: a share's
trades depend on its own candles and the benchmark, nothing else. The
portfolio pass (``simulate_portfolio``) decides each session on its own: no
state crosses midnight. So a long replay splits exactly:

* by month -- each month is loaded with enough earlier sessions for the daily
  history and the volume baseline (``warmup_days``) and replayed only from its
  first day (``evaluate_from``), so its signals are the ones a single pass
  over the whole period would emit;
* by share -- one share's month is in memory at a time, with the benchmark's;
* the portfolio pass runs on one month's candidates and keeps only the taken
  trades, which are at most a few a day.

Memory is then one share-month of candles plus one month of candidates,
whatever the length of the period or the size of the universe. The trades are
the same as one pass over everything; ``tests/test_research_replay.py``
holds the two against each other.

Research only. Nothing here reaches a broker, the scanner or a setting.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchDataset
from app.services.backtesting import run_completed_candle_backtest
from app.services.paper_execution import PaperExecutionControls
from app.services.portfolio_simulation import simulate_portfolio
from app.services.research_data import _files, _files_by_key, data_root, month_ranges, window
from app.services.strategy_registry import StrategyConfiguration

# Calendar days a period needs before it, at the least. Weekends and holidays
# make a session about 1.45 calendar days.
MINIMUM_WARMUP_DAYS = 70


def warmup_days(settings: object) -> int:
    """Calendar days of earlier bars a month needs for its first session's indicators.

    The replay looks back ``daily_history_sessions`` sessions for daily
    candles and ``rvol_baseline_sessions`` for the volume baseline; intraday
    indicators use the session alone.
    """
    sessions = max(
        int(getattr(settings, "daily_history_sessions", 40)),
        int(getattr(settings, "rvol_baseline_sessions", 10)),
    )
    return max(MINIMUM_WARMUP_DAYS, math.ceil(sessions * 1.45) + 15)


@dataclass
class ReplayGroup:
    """Strategies that can share one replay: the same controls and indicator settings.

    ``index_of`` maps each strategy's id to the index the caller knows it by.
    """

    strategies: list[StrategyConfiguration]
    controls: dict
    settings: object
    index_of: dict[str, int]


@dataclass
class ReplayStats:
    months: int = 0
    share_months: int = 0
    candidates: int = 0
    candles: int = 0
    largest_share_month: int = 0
    skipped_months: list[str] = field(default_factory=list)
    # Per index: the portfolio pass's tallies, summed over the months.
    portfolio: dict[int, dict] = field(default_factory=dict)

    def add_outcome(self, index: int, outcome) -> None:  # noqa: ANN001 - PortfolioOutcome
        tally = self.portfolio.setdefault(
            index,
            {
                "candidates": 0,
                "taken": 0,
                "refused": 0,
                "superseded": 0,
                "refused_by_reason": Counter(),
                "halted_sessions": 0,
            },
        )
        tally["candidates"] += outcome.candidates
        tally["taken"] += len(outcome.taken)
        tally["refused"] += len(outcome.refused)
        tally["superseded"] += outcome.superseded
        tally["refused_by_reason"].update(outcome.refusal_counts())
        tally["halted_sessions"] += len(outcome.halted_sessions)

    def portfolio_summary(self, index: int) -> dict:
        tally = self.portfolio.get(index)
        if tally is None:
            return {
                "candidates": 0,
                "taken": 0,
                "refused": 0,
                "superseded": 0,
                "refused_by_reason": {},
                "halted_sessions": 0,
            }
        return {**tally, "refused_by_reason": dict(sorted(tally["refused_by_reason"].items()))}

    def as_dict(self) -> dict:
        return {
            "months": self.months,
            "share_months": self.share_months,
            "candles_replayed": self.candles,
            "largest_share_month_candles": self.largest_share_month,
            "skipped_months": self.skipped_months,
        }


async def replay_taken(
    session: AsyncSession,
    dataset: ResearchDataset,
    instruments: list[str],
    start: date,
    end: date,
    groups: list[ReplayGroup],
    execution: PaperExecutionControls,
    *,
    root: Path | None = None,
    sector_of_token: Callable[[str], str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[int, list], ReplayStats]:
    """The trades each strategy would have taken, as its own live account, from ``start`` to ``end``.

    Returns {index: taken trades in exit order} and how the work was split.
    """
    root = root or data_root()
    grouped = _files_by_key(await _files(session, dataset.id))
    benchmark_rows = grouped.get(dataset.benchmark_key, [])
    warm = max(warmup_days(group.settings) for group in groups)
    taken: dict[int, list] = defaultdict(list)
    stats = ReplayStats()

    for month, first, last in month_ranges(start, end):
        load_from = first - timedelta(days=warm)
        benchmark = window(root, dataset, benchmark_rows, load_from, last)
        if not any(candle.session_date >= first for candle in benchmark):
            stats.skipped_months.append(f"{month:%Y-%m}")
            continue
        stats.months += 1
        candidates: list[list] = [[] for _ in groups]
        for key in instruments:
            rows = window(root, dataset, grouped.get(key, []), load_from, last)
            if not rows:
                continue
            stats.share_months += 1
            stats.candles += len(rows)
            stats.largest_share_month = max(stats.largest_share_month, len(rows))
            for position, group in enumerate(groups):
                replay = run_completed_candle_backtest(
                    {key: rows},
                    benchmark,
                    group.strategies,
                    group.controls,
                    execution,
                    group.settings,
                    all_signals=True,
                    evaluate_from=first,
                )
                candidates[position].extend(replay.trades)
            del rows
        for position, group in enumerate(groups):
            stats.candidates += len(candidates[position])
            for strategy in group.strategies:
                mine = [trade for trade in candidates[position] if trade.strategy_id == strategy.id]
                outcome = simulate_portfolio(mine, group.controls, [strategy], sector_of_token=sector_of_token)
                taken[group.index_of[strategy.id]].extend(outcome.taken)
                stats.add_outcome(group.index_of[strategy.id], outcome)
        if progress is not None:
            progress(f"{month:%Y-%m}: {stats.share_months} share-months, {stats.candidates} candidates so far")

    for trades in taken.values():
        trades.sort(key=lambda trade: (trade.exited_at, trade.trade_key))
    return dict(taken), stats
