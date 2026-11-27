"""Research Lab Phase 4: historical datasets that can be trusted and reproduced.

The claims:

* the provider is asked without credentials, politely, and retried only when
  retrying can help;
* files are deterministic, so the same bars always hash the same;
* a backfill is resumable and never rewrites a finished month it already has;
* coverage measures each share against the benchmark's own sessions, and
  reports what is missing rather than filling it in;
* a finalised dataset is immutable and detects a file changed after the fact;
* a backtest can run on a dataset, and says which one.
"""

import gzip
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.db.models import ResearchDataset, ResearchDatasetCoverage, ResearchDatasetFile
from app.db.session import SessionLocal
from app.services import research_data as rd
from app.services import research_runs

IST = timezone(timedelta(hours=5, minutes=30))
STOCK = "NSE_EQ|INE002A01018"


def _bar(day: date, hh: int, mm: int, close: str = "100", volume: int = 10) -> rd.Bar:
    price = Decimal(close)
    return rd.Bar(
        datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST), price, price + 1, price - 1, price, volume
    )


def _session(day: date, minutes: int = 5, start: int = 0) -> list[rd.Bar]:
    return [_bar(day, 9, 15 + i) for i in range(start, minutes)]


class FakeSource:
    """Serves fixed bars per (instrument, month) and counts what was asked."""

    def __init__(self, bars: dict[str, list[rd.Bar]]):
        self.bars = bars
        self.requests = 0
        self.asked: list[tuple[str, date, date]] = []

    async def fetch(self, key, timeframe, start, end):
        self.requests += 1
        self.asked.append((key, start, end))
        return [bar for bar in self.bars.get(key, []) if start <= bar.at.date() <= end]


@pytest.fixture
async def clean_datasets():
    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(ResearchDatasetCoverage))
            await session.execute(delete(ResearchDatasetFile))
            await session.execute(delete(ResearchDataset))
            await session.commit()

    await wipe()
    yield
    await wipe()


# --- the provider ------------------------------------------------------------------------------


def test_parse_puts_the_oldest_bar_first_and_drops_open_interest():
    bars = rd.parse_candles(
        [
            ["2022-01-31T15:29:00+05:30", 1135.2, 1136.7, 1134.8, 1136.25, 40256, 0],
            ["2022-01-31T15:28:00+05:30", 1135, 1135.5, 1134.5, 1135.1, 100, 0],
        ]
    )
    assert [bar.at.minute for bar in bars] == [28, 29]
    assert bars[1].close == Decimal("1136.25") and bars[1].volume == 40256


def test_month_ranges_clip_and_cross_the_year():
    assert rd.month_ranges(date(2021, 12, 15), date(2022, 2, 3)) == [
        (date(2021, 12, 1), date(2021, 12, 15), date(2021, 12, 31)),
        (date(2022, 1, 1), date(2022, 1, 1), date(2022, 1, 31)),
        (date(2022, 2, 1), date(2022, 2, 1), date(2022, 2, 3)),
    ]


async def test_the_client_sends_no_credentials_encodes_the_key_and_retries_only_what_helps():
    seen = []
    replies = iter(
        [
            httpx.Response(429),
            httpx.Response(
                200,
                json={"status": "success", "data": {"candles": [["2022-01-03T09:15:00+05:30", 1, 2, 0.5, 1.5, 7, 0]]}},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(replies)

    async def no_sleep(_seconds):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = rd.UpstoxPublicHistory(client, sleep=no_sleep)
        bars = await source.fetch(STOCK, "1minute", date(2022, 1, 1), date(2022, 1, 31))
    assert len(bars) == 1 and source.requests == 2
    assert "NSE_EQ%7CINE002A01018/minutes/1/2022-01-31/2022-01-01" in str(seen[0].url)
    assert all("authorization" not in {k.lower() for k in request.headers} for request in seen)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400))) as client:
        with pytest.raises(rd.SourceError):
            await rd.UpstoxPublicHistory(client, sleep=no_sleep).fetch(
                STOCK, "1minute", date(2022, 1, 1), date(2022, 1, 2)
            )
    refused = httpx.Response(200, json={"status": "error", "errors": [{"message": "bad key"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: refused)) as client:
        with pytest.raises(rd.SourceError):
            await rd.UpstoxPublicHistory(client, sleep=no_sleep).fetch(
                STOCK, "1minute", date(2022, 1, 1), date(2022, 1, 2)
            )


# --- files and checks ------------------------------------------------------------------------------


def test_files_are_deterministic_and_round_trip():
    bars = _session(date(2022, 1, 3))
    first, second = rd.encode_bars(bars), rd.encode_bars(list(reversed(bars)))
    assert first == second and hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
    assert rd.decode_bars(first) == bars
    with pytest.raises(ValueError):
        rd.decode_bars(gzip.compress(b"a,b\n1,2\n"))


def test_check_day_counts_what_is_wrong():
    day = date(2022, 1, 3)
    good = _session(day, 3)
    bad = rd.Bar(good[0].at + timedelta(minutes=10), Decimal("10"), Decimal("9"), Decimal("8"), Decimal("10"), 0)
    late = _bar(day, 15, 45)
    check = rd.check_day([*good, good[0], bad, late])
    assert (check.bars, check.duplicates, check.bad_ohlc, check.zero_volume, check.outside_session) == (5, 1, 1, 1, 1)


# --- datasets ------------------------------------------------------------------------------------


async def _dataset(name="t-1m", start=date(2022, 1, 1), end=date(2022, 2, 28)):
    async with SessionLocal() as session:
        dataset = await rd.open_dataset(session, name=name, timeframe="1minute", start=start, end=end)
        await session.commit()
        return dataset.id


async def test_backfill_writes_benchmark_first_and_resumes(clean_datasets, tmp_path):
    days = [date(2022, 1, 3), date(2022, 1, 4), date(2022, 2, 1)]
    source = FakeSource(
        {rd.NIFTY_50: [b for d in days for b in _session(d)], STOCK: [b for d in days for b in _session(d)]}
    )
    dataset_id = await _dataset()
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        report = await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
    assert source.asked[0][0] == rd.NIFTY_50
    assert report.files_written == 4 and report.bars_written == 30 and report.empty_months == 0

    again = FakeSource(source.bars)
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        second = await rd.backfill(session, dataset, [STOCK], again, root=tmp_path, today=date(2026, 11, 27))
    assert again.requests == 0 and second.files_kept == 4  # finished months are never fetched twice

    (tmp_path / rd.file_path("t-1m", STOCK, date(2022, 1, 1))).write_bytes(b"tampered")
    third = FakeSource(source.bars)
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        await rd.backfill(session, dataset, [STOCK], third, root=tmp_path, today=date(2026, 11, 27))
    assert third.asked == [(STOCK, date(2022, 1, 1), date(2022, 1, 31))]  # only the damaged file is fetched again


async def test_the_current_month_is_always_fetched_again(clean_datasets, tmp_path):
    source = FakeSource({})
    dataset_id = await _dataset(start=date(2026, 11, 1), end=date(2026, 11, 26))
    for _ in range(2):
        async with SessionLocal() as session:
            dataset = await session.get(ResearchDataset, dataset_id)
            await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
    assert source.requests == 4  # benchmark + stock, twice: November may still be incomplete


async def test_a_closed_window_stops_between_files(clean_datasets, tmp_path):
    source = FakeSource({})
    dataset_id = await _dataset()
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        report = await rd.backfill(
            session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27), keep_going=lambda: False
        )
    assert source.requests == 0 and "window closed" in report.stopped_early


async def test_coverage_measures_against_the_benchmark_sessions(clean_datasets, tmp_path):
    d1, d2, d3 = date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)
    source = FakeSource(
        {
            rd.NIFTY_50: [*_session(d1), *_session(d2), *_session(d3, 3)],  # d3 a short special session
            STOCK: [*_session(d1), *_session(d2, 5, 2), _session(d1)[0]],  # d2 missing two bars, a duplicate, d3 absent
        }
    )
    dataset_id = await _dataset(end=date(2022, 1, 31))
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
        await rd.compute_coverage(session, dataset, root=tmp_path)
        await session.commit()
        rows = {
            row.session_date: row
            for row in (
                await session.scalars(
                    select(ResearchDatasetCoverage).where(
                        ResearchDatasetCoverage.dataset_id == dataset_id,
                        ResearchDatasetCoverage.instrument_key == STOCK,
                    )
                )
            ).all()
        }
    assert (rows[d1].bars, rows[d1].missing_bars, rows[d1].duplicates) == (5, 0, 1)
    assert (rows[d2].bars, rows[d2].expected_bars, rows[d2].missing_bars) == (3, 5, 2)
    assert (rows[d3].bars, rows[d3].expected_bars, rows[d3].missing_bars) == (0, 3, 3)


async def test_finalise_fixes_a_hash_and_then_refuses_changes(clean_datasets, tmp_path):
    source = FakeSource({rd.NIFTY_50: _session(date(2022, 1, 3)), STOCK: _session(date(2022, 1, 3))})
    dataset_id = await _dataset(end=date(2022, 1, 31))
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
        digest = await rd.finalize(session, dataset, root=tmp_path)
        await session.commit()
    assert len(digest) == 64
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        assert dataset.status == "COMPLETE" and dataset.row_count == 10 and dataset.instrument_count == 2
        with pytest.raises(ValueError, match="immutable"):
            await rd.open_dataset(
                session, name="t-1m", timeframe="1minute", start=date(2022, 1, 1), end=date(2022, 1, 31)
            )

    (tmp_path / rd.file_path("t-1m", STOCK, date(2022, 1, 1))).write_bytes(b"changed")
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="does not match"):
            await rd.read_dataset(session, dataset_id, [STOCK], date(2022, 1, 1), date(2022, 1, 31), root=tmp_path)


async def test_read_dataset_gives_candles_without_duplicates_inside_the_range(clean_datasets, tmp_path):
    d1, d2 = date(2022, 1, 3), date(2022, 1, 4)
    stock = [*_session(d1), _session(d1)[0], *_session(d2)]
    source = FakeSource({rd.NIFTY_50: [*_session(d1), *_session(d2)], STOCK: stock})
    dataset_id = await _dataset(end=date(2022, 1, 31))
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
        _, by_instrument, benchmark = await rd.read_dataset(session, dataset_id, [STOCK], d1, d1, root=tmp_path)
    assert len(by_instrument[STOCK]) == 5 and len(benchmark) == 5
    first = by_instrument[STOCK][0]
    assert first.timeframe_seconds == 60 and first.closed_at - first.opened_at == timedelta(minutes=1)
    assert first.opened_at.tzinfo is not None and first.session_date == d1


# --- runs on a dataset ------------------------------------------------------------------------------


async def test_a_run_refuses_an_unfinished_or_mismatched_dataset_and_names_a_finished_one(
    clean_datasets, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path))
    source = FakeSource({rd.NIFTY_50: _session(date(2022, 1, 3)), STOCK: _session(date(2022, 1, 3))})
    dataset_id = await _dataset(end=date(2022, 1, 31))
    request = {"dataset_id": str(dataset_id)}
    async with SessionLocal() as session:
        dataset = await session.get(ResearchDataset, dataset_id)
        await rd.backfill(session, dataset, [STOCK], source, root=tmp_path, today=date(2026, 11, 27))
        with pytest.raises(research_runs.ResearchInputError, match="BUILDING"):
            await research_runs.candles_for(session, None, request, [STOCK], 60, date(2022, 1, 3), date(2022, 1, 3))
        await rd.finalize(session, dataset, root=tmp_path)
        await session.commit()
        with pytest.raises(research_runs.ResearchInputError, match="1minute"):
            await research_runs.candles_for(session, None, request, [STOCK], 300, date(2022, 1, 3), date(2022, 1, 3))
        by_instrument, benchmark, provenance = await research_runs.candles_for(
            session, None, request, [STOCK], 60, date(2022, 1, 3), date(2022, 1, 3)
        )
    assert len(by_instrument[STOCK]) == 5 and benchmark
    assert provenance["dataset_name"] == "t-1m" and provenance["price_adjustment"] == rd.ADJUSTED_BY_PROVIDER
    assert len(provenance["content_sha256"]) == 64


async def test_the_backfill_job_builds_and_finalises_a_dataset(clean_datasets, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path))
    days = [date(2022, 1, 3), date(2022, 1, 4)]
    fake = FakeSource(
        {rd.NIFTY_50: [b for d in days for b in _session(d)], STOCK: [b for d in days for b in _session(d)]}
    )
    monkeypatch.setattr(rd, "UpstoxPublicHistory", lambda **_kwargs: fake)
    monkeypatch.setattr("app.services.research_jobs.research_allowed", lambda *_: SimpleNamespace(open=True))
    settings = SimpleNamespace(nse_holiday_overrides="", nse_special_sessions="", calendar_confirmed_years={2026})
    payload = {
        "dataset": "job-1m",
        "timeframe": "1minute",
        "start": "2022-01-01",
        "end": "2022-01-31",
        "instruments": [STOCK],
    }
    async with SessionLocal() as session:
        result = await research_runs.run_backfill_job(session, payload, settings, None)
        await session.commit()
    assert result["files_written"] == 2 and result["bars_written"] == 20 and len(result["content_sha256"]) == 64
    assert result["coverage_rows"] == 4
    json.dumps(result)  # stored on the job as JSON


def test_the_worker_volume_and_network_for_backfills():
    from pathlib import Path

    compose_file = Path(__file__).resolve().parents[1].parents[1] / "docker-compose.yml"
    if not compose_file.exists():
        pytest.skip("needs the repository checkout (docker-compose.yml); the test container holds apps/api only")
    compose = compose_file.read_text(encoding="utf-8")
    block = compose.split("  research-worker:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert "research_data:/app/research_data" in block and "RESEARCH_DATA_DIR: /app/research_data" in block
    assert "\n  research_data:\n" in compose.split("\nvolumes:\n", 1)[1] + "\n"


async def test_a_job_running_when_the_window_closes_is_queued_again_without_spending_an_attempt():
    from app import research_worker
    from app.db.models import ResearchJob, ResearchJobEvent
    from app.services import research_jobs

    class LongProcess:
        exitcode = None

        def start(self):
            pass

        def is_alive(self):
            return True

        def terminate(self):
            self.exitcode = -15

        def join(self, _timeout=None):
            pass

    async with SessionLocal() as session:
        await session.execute(delete(ResearchJobEvent))
        await session.execute(delete(ResearchJob))
        job = await research_jobs.enqueue(session, "backfill", {}, user_id=None)
        await session.commit()
        job_id = job.id
    async with SessionLocal() as session:
        await research_jobs.claim_next(session, "w1")
        await session.commit()
    status = await research_worker.supervise(job_id, 3600, "w1", process_factory=LongProcess, window_open=lambda: False)
    async with SessionLocal() as session:
        row = await session.get(ResearchJob, job_id)
        assert status == research_jobs.QUEUED and row.status == research_jobs.QUEUED and row.attempts == 0
        await session.execute(delete(ResearchJobEvent))
        await session.execute(delete(ResearchJob))
        await session.commit()


def test_the_universe_import_refuses_malformed_rows_and_marks_survivorship():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "research_import_universe.py"
    spec = importlib.util.spec_from_file_location("research_import_universe_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_universe([["NSE_EQ|INE466L01038", "360one", "Financial Services"]]) == [
        ("NSE_EQ|INE466L01038", "360ONE", "Financial Services")
    ]
    for bad in ([["RELIANCE"]], [["RELIANCE", "RELIANCE"]], ["not-a-row"]):
        with pytest.raises(ValueError):
            module.parse_universe(bad)
    assert "survivorship" in module.CURRENT_ONLY


def test_coverage_summary_counts_complete_and_absent_sessions():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "research_coverage.py"
    spec = importlib.util.spec_from_file_location("research_coverage_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    totals = module.summarise(
        [
            ("A", 375, 375, 0, 0, 0, 0, 0),
            ("A", 375, 300, 75, 1, 0, 2, 0),
            ("A", 375, 0, 375, 0, 0, 0, 0),
            ("A", 0, 5, 0, 0, 0, 0, 5),
        ]
    )
    assert totals["A"] == {
        "sessions": 3,
        "complete": 1,
        "absent": 1,
        "missing": 450,
        "duplicates": 1,
        "bad_ohlc": 0,
        "zero_volume": 2,
        "outside": 5,
    }


def test_a_2022_replay_has_indicators_only_with_the_dataset_calendar():
    """The trap found on the first real-data run: the app calendar knows only 2026.

    Without ``replay_calendar`` every 2022 candle is "not a session", no EMA or
    ATR is computed, and every strategy refuses every candle -- a backtest
    that silently never trades.
    """
    from app.services import market_calculations
    from app.services.market_calculations import CompletedCandle, indicator_snapshot

    day = date(2022, 1, 3)
    candles = [
        CompletedCandle(
            instrument_token=STOCK,
            timeframe_seconds=60,
            opened_at=bar.at,
            closed_at=bar.at + timedelta(minutes=1),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            tick_count=0,
        )
        for bar in _session(day, 40)
    ]

    def snapshot():
        return indicator_snapshot(
            candles,
            candles,
            opening_range_minutes=15,
            fast_ema_period=9,
            slow_ema_period=21,
            volume_lookback=20,
            is_nifty=False,
            atr_period=14,
        )

    assert snapshot()["ema_fast"] is None  # the default calendar refuses 2022
    before = market_calculations.DEFAULT_TRADING_CALENDAR
    with rd.replay_calendar({day}):
        assert snapshot()["ema_fast"] is not None
    assert market_calculations.DEFAULT_TRADING_CALENDAR is before  # restored
