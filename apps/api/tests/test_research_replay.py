"""A long research replay a month at a time, in bounded memory, with the same trades as one pass.

The claims:

* ``evaluate_from`` replays only later sessions, while earlier ones still feed
  the daily history: the later trades are identical to a full replay's;
* ``replay_taken`` (one share and one month at a time, the portfolio pass per
  month) takes exactly the trades ``run_portfolio_backtest`` takes over the
  whole period at once -- across a month boundary, for several shares and
  several variants;
* a dataset read that would not fit the research worker is refused before
  anything is loaded;
* coverage is still computed correctly one share at a time.
"""

import hashlib
import random
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models import ResearchDataset, ResearchDatasetCoverage, ResearchDatasetFile
from app.db.session import SessionLocal
from app.services import research_data as rd
from app.services.backtesting import run_completed_candle_backtest
from app.services.market_calculations import CompletedCandle
from app.services.portfolio_simulation import run_portfolio_backtest
from app.services.research_replay import ReplayGroup, replay_taken, warmup_days
from app.services.trading_calendar import MARKET_TIMEZONE
from tests.test_backtest_sweep import _EXECUTION, _controls, _strategy

BENCHMARK = "NSE_INDEX|Nifty 50"
SHARES = ["NSE_EQ|AAA", "NSE_EQ|BBB", "NSE_EQ|CCC"]
SETTINGS = SimpleNamespace(
    opening_range_minutes=15,
    ema_fast_period=9,
    ema_slow_period=21,
    volume_lookback_candles=20,
    atr_period=14,
    daily_history_sessions=40,
    rvol_baseline_sessions=10,
)


def _sessions(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


DAYS = _sessions(date(2026, 1, 19), date(2026, 2, 13))
MINUTES = 60


def _series(key: str, seed: int, minutes: int = MINUTES) -> list[CompletedCandle]:
    """A seeded random walk, an hour a session, with drift so setups form."""
    rng = random.Random(seed)
    price = Decimal("500")
    rows = []
    for day in DAYS:
        start = datetime.combine(day, time(9, 15), tzinfo=MARKET_TIMEZONE).astimezone(UTC)
        for i in range(minutes):
            step = Decimal(str(round(rng.gauss(0.05, 0.9), 2)))
            open_ = price
            close = max(Decimal("50"), price + step)
            high = max(open_, close) + Decimal(str(round(abs(rng.gauss(0, 0.4)), 2)))
            low = min(open_, close) - Decimal(str(round(abs(rng.gauss(0, 0.4)), 2)))
            opened = start + timedelta(minutes=i)
            rows.append(
                CompletedCandle(
                    instrument_token=key,
                    timeframe_seconds=60,
                    opened_at=opened,
                    closed_at=opened + timedelta(minutes=1),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=rng.randint(500, 5000),
                    tick_count=0,
                )
            )
            price = close
    return rows


def _universe() -> tuple[dict[str, list[CompletedCandle]], list[CompletedCandle]]:
    return {key: _series(key, seed) for seed, key in enumerate(SHARES, start=1)}, _series(BENCHMARK, 99)


def _strategies():
    loose = _strategy().model_copy(update={"universe": SHARES, "id": "loose"})
    strict = _strategy().model_copy(update={"universe": SHARES, "id": "strict", "minimum_rr": 2})
    return [loose, strict]


async def _store(session, dataset: ResearchDataset, series: dict[str, list[CompletedCandle]], root) -> None:
    """Write candles as the backfill would: one gzip CSV per instrument and month, hashed."""
    for key, rows in series.items():
        by_month = defaultdict(list)
        for candle in rows:
            local = candle.opened_at.astimezone(MARKET_TIMEZONE)
            bar = rd.Bar(local, candle.open, candle.high, candle.low, candle.close, candle.volume)
            by_month[date(local.year, local.month, 1)].append(bar)
        for month, bars in by_month.items():
            payload = rd.encode_bars(bars)
            relative = rd.file_path(dataset.name, key, month)
            rd._write_atomically(root, relative, payload)
            session.add(
                ResearchDatasetFile(
                    dataset_id=dataset.id,
                    instrument_key=key,
                    month=month,
                    relative_path=relative,
                    rows=len(bars),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    first_bar_at=bars[0].at,
                    last_bar_at=bars[-1].at,
                    fetched_at=datetime.now(UTC),
                )
            )
    await session.flush()


@pytest.fixture
async def stored(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path))

    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(ResearchDataset).where(ResearchDataset.name.like("replay-test%")))
            await session.commit()

    await wipe()
    series, benchmark = _universe()
    async with SessionLocal() as session:
        dataset = ResearchDataset(
            name="replay-test",
            source="test",
            timeframe="1minute",
            price_adjustment="RAW",
            benchmark_key=BENCHMARK,
            start_date=DAYS[0],
            end_date=DAYS[-1],
            status="COMPLETE",
            storage_path="replay-test",
            content_sha256="e" * 64,
            provenance={},
        )
        session.add(dataset)
        await session.flush()
        await _store(session, dataset, {**series, BENCHMARK: benchmark}, tmp_path)
        await session.commit()
        dataset_id = dataset.id
    yield SimpleNamespace(id=dataset_id, root=tmp_path, series=series, benchmark=benchmark)
    await wipe()


def _key(trade):
    return (trade.trade_key, trade.exited_at, trade.exit_price, trade.net_pnl, trade.realized_r, trade.exit_reason)


def test_evaluate_from_keeps_later_trades_identical():
    series, benchmark = _universe()
    strategies = _strategies()
    full = run_completed_candle_backtest(
        series, benchmark, strategies, _controls(), _EXECUTION, SETTINGS, all_signals=True
    )
    cut = date(2026, 2, 2)
    later = run_completed_candle_backtest(
        series, benchmark, strategies, _controls(), _EXECUTION, SETTINGS, all_signals=True, evaluate_from=cut
    )
    expected = [_key(t) for t in full.trades if date.fromisoformat(t.session_date) >= cut]
    assert expected, "the fixture must produce trades after the cut or the test proves nothing"
    assert [_key(t) for t in later.trades] == expected
    assert all(date.fromisoformat(t.session_date) >= cut for t in later.trades)


async def test_month_at_a_time_takes_exactly_what_one_pass_takes(stored):
    strategies = _strategies()
    controls = {**_controls(), "max_trades_per_day": 2}
    one_pass = {}
    for strategy in strategies:
        result, _ = run_portfolio_backtest(stored.series, stored.benchmark, [strategy], controls, _EXECUTION, SETTINGS)
        one_pass[strategy.id] = [_key(t) for t in result.trades]
    group = ReplayGroup(
        strategies=strategies,
        controls=controls,
        settings=SETTINGS,
        index_of={strategy.id: index for index, strategy in enumerate(strategies)},
    )
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, stored.id)
        taken, replay_stats = await replay_taken(
            session, dataset, SHARES, DAYS[0], DAYS[-1], [group], _EXECUTION, root=stored.root
        )

    months_with_trades = {t.session_date[:7] for trades in taken.values() for t in trades}
    assert len(months_with_trades) >= 2, "the comparison must cross a month boundary"
    for index, strategy in enumerate(strategies):
        assert [_key(t) for t in taken.get(index, [])] == one_pass[strategy.id], strategy.id
    assert replay_stats.months == 2 and replay_stats.share_months == 6
    # One share's month plus its warm-up at a time (here the warm-up spans the whole fixture).
    assert replay_stats.largest_share_month == len(stored.series[SHARES[0]])
    summary = replay_stats.portfolio_summary(0)
    assert summary["taken"] == len(taken.get(0, [])) and summary["candidates"] >= summary["taken"]


async def test_replay_reads_nothing_past_the_end(stored, monkeypatch):
    from app.services import research_replay

    asked = []
    real = research_replay.window

    def spy(root, dataset, rows, start, end):
        asked.append(end)
        return real(root, dataset, rows, start, end)

    monkeypatch.setattr(research_replay, "window", spy)
    group = ReplayGroup(strategies=_strategies()[:1], controls=_controls(), settings=SETTINGS, index_of={"loose": 0})
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, stored.id)
        await replay_taken(session, dataset, SHARES, DAYS[0], date(2026, 2, 4), [group], _EXECUTION)
    assert asked and max(asked) == date(2026, 2, 4)


async def test_an_oversized_read_is_refused_before_loading(stored):
    async with SessionLocal() as session:
        with pytest.raises(rd.DatasetTooLarge, match="at most 1,000"):
            await rd.read_dataset(session, stored.id, SHARES, DAYS[0], DAYS[-1], max_candles=1_000)
        dataset, by_instrument, benchmark = await rd.read_dataset(session, stored.id, SHARES[:1], DAYS[0], DAYS[4])
    assert len(by_instrument[SHARES[0]]) == 5 * MINUTES and len(benchmark) == 5 * MINUTES


async def test_benchmark_days_and_coverage_one_share_at_a_time(stored):
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, stored.id)
        days = await rd.benchmark_days(session, dataset, DAYS[0], DAYS[-1])
        assert list(days) == DAYS and set(days.values()) <= {"UP", "DOWN"}
        written = await rd.compute_coverage(session, dataset)
        await session.commit()
        rows = (
            await session.scalars(
                select(ResearchDatasetCoverage).where(ResearchDatasetCoverage.dataset_id == dataset.id)
            )
        ).all()
    assert written == len(rows) == len(DAYS) * (len(SHARES) + 1)
    assert all(row.bars == MINUTES and row.expected_bars == MINUTES and row.missing_bars == 0 for row in rows)


def test_warmup_covers_the_longest_lookback():
    assert warmup_days(SETTINGS) == 73
    assert warmup_days(SimpleNamespace(daily_history_sessions=100, rvol_baseline_sessions=10)) == 160


def test_the_replay_calendar_remembers_answers_without_changing_them():
    calendar = rd.DatasetCalendar([date(2022, 3, 2)])
    calendar.REMEMBERED = 3
    open_minute = datetime(2022, 3, 2, 9, 30, tzinfo=MARKET_TIMEZONE)
    instants = [
        open_minute,
        open_minute.replace(hour=8),
        open_minute.replace(day=3),
        open_minute.replace(hour=15, minute=29),
    ]
    first = [calendar.is_open(at) for at in instants]
    assert first == [True, False, False, True]
    assert [calendar.is_open(at) for at in instants] == first
    assert len(calendar._open_at) <= 3  # bounded: cleared when full
