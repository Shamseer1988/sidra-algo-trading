"""Historical bars for research: fetched, checked, stored as files, and read back.

**Where they come from.** Upstox's public v3 historical-candle endpoint, which
needs no credentials (the research worker holds none). Measured on 27 November
2026 with RELIANCE:

* 1-minute bars start in January 2022. Every month before that returns
  ``status: success`` with zero candles, indistinguishable from a month with no
  trading -- so zero bars is recorded as "not available", never as a holiday.
* The current session is not included; the endpoint is end-of-day history.
* No rate-limit headers are returned. This client stays at two requests a
  second and backs off on 429 and 5xx.
* **Prices are adjusted for corporate actions by the provider.** RELIANCE
  closed near Rs 1,135 on 31 January 2022 in this data; it traded near
  Rs 2,400 that day, before the October 2024 1:1 bonus. So these bars do not
  match the unadjusted candles SIDRA recorded live, and a dataset says which
  it is in ``price_adjustment``. Whether volume is adjusted too is not
  verified.

**Where they go.** One gzip CSV per instrument and month under the research
worker's volume, written deterministically (fixed column order, sorted by time,
gzip timestamp zero) so the same bars always give the same SHA-256. Bars are
stored as the provider sent them, duplicates included; readers drop a repeated
timestamp and coverage counts it. Nothing is written to
``public.market_candles``.

**How they are judged.** A trading session is a day the benchmark (NIFTY 50)
has bars in the same dataset, and its expected bar count is the benchmark's
count that day -- which also handles a short special session without a
holiday list for the year, which the calendar does not have before 2026.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import io
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchDataset, ResearchDatasetCoverage, ResearchDatasetFile
from app.services.market_calculations import CompletedCandle
from app.services.trading_calendar import MARKET_TIMEZONE, TradingCalendar, TradingSession

UPSTOX_PUBLIC_SOURCE = "upstox-public-v3"
UPSTOX_HISTORY_URL = "https://api.upstox.com/v3/historical-candle/{key}/{unit}/{interval}/{to}/{start}"
ADJUSTED_BY_PROVIDER = "ADJUSTED_BY_PROVIDER"
NIFTY_50 = "NSE_INDEX|Nifty 50"

# name -> (Upstox unit, interval, seconds per bar)
TIMEFRAMES = {
    "1minute": ("minutes", 1, 60),
    "5minute": ("minutes", 5, 300),
    "day": ("days", 1, 86_400),
}
SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)
COLUMNS = ("at", "open", "high", "low", "close", "volume")
DEFAULT_ROOT = "/app/research_data"


def data_root() -> Path:
    return Path(os.environ.get("RESEARCH_DATA_DIR", DEFAULT_ROOT))


class SourceError(RuntimeError):
    """The provider refused a request in a way retrying will not fix."""


@dataclass(frozen=True)
class Bar:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


# --- the provider ---------------------------------------------------------------------


class UpstoxPublicHistory:
    """The public, unauthenticated historical-candle endpoint. Sends no credentials, ever."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        requests_per_second: float = 2.0,
        max_retries: int = 4,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._interval = 1.0 / requests_per_second
        self._max_retries = max_retries
        self._sleep = sleep
        self._last = 0.0
        self.requests = 0

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        wait = self._last + self._interval - loop.time()
        if wait > 0:
            await self._sleep(wait)
        self._last = loop.time()

    async def fetch(self, instrument_key: str, timeframe: str, start: date, end: date) -> list[Bar]:
        unit, interval, _ = TIMEFRAMES[timeframe]
        url = UPSTOX_HISTORY_URL.format(
            key=quote(instrument_key, safe=""),
            unit=unit,
            interval=interval,
            to=end.isoformat(),
            start=start.isoformat(),
        )
        client = self._client or httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "sidra-research"})
        try:
            for attempt in range(self._max_retries + 1):
                await self._pace()
                self.requests += 1
                try:
                    response = await client.get(url)
                except httpx.TransportError:
                    if attempt == self._max_retries:
                        raise
                    await self._sleep(2**attempt)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self._max_retries:
                        raise SourceError(f"{instrument_key} {start}..{end}: HTTP {response.status_code} after retries")
                    await self._sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    raise SourceError(f"{instrument_key} {start}..{end}: HTTP {response.status_code}")
                body = response.json()
                if body.get("status") != "success":
                    raise SourceError(f"{instrument_key} {start}..{end}: {body.get('errors') or body.get('status')}")
                return parse_candles(((body.get("data") or {}).get("candles")) or [])
            raise SourceError("unreachable")
        finally:
            if self._client is None:
                await client.aclose()


def parse_candles(rows: Iterable[list]) -> list[Bar]:
    """Provider rows, newest first, to bars oldest first. Open interest is dropped."""
    bars = [
        Bar(
            at=datetime.fromisoformat(str(row[0])),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=int(row[5] or 0),
        )
        for row in rows
    ]
    return sorted(bars, key=lambda bar: bar.at)


def month_ranges(start: date, end: date) -> list[tuple[date, date, date]]:
    """(month, first day to ask for, last day to ask for), clipped to the requested range."""
    ranges = []
    month = date(start.year, start.month, 1)
    while month <= end:
        following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        ranges.append((month, max(month, start), min(following - timedelta(days=1), end)))
        month = following
    return ranges


# --- files ------------------------------------------------------------------------------


def encode_bars(bars: list[Bar]) -> bytes:
    """Deterministic gzip CSV: same bars, same bytes, same hash."""
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(COLUMNS)
    for bar in sorted(bars, key=lambda item: item.at):
        writer.writerow([bar.at.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as handle:
        handle.write(text.getvalue().encode("utf-8"))
    return buffer.getvalue()


def decode_bars(payload: bytes) -> list[Bar]:
    rows = csv.reader(io.StringIO(gzip.decompress(payload).decode("utf-8")))
    header = next(rows, None)
    if tuple(header or ()) != COLUMNS:
        raise ValueError(f"Unexpected columns {header!r}")
    return [
        Bar(datetime.fromisoformat(at), Decimal(o), Decimal(h), Decimal(lo), Decimal(c), int(v))
        for at, o, h, lo, c, v in rows
    ]


def file_path(dataset_name: str, instrument_key: str, month: date) -> str:
    safe = instrument_key.replace("|", "_").replace("/", "_").replace(" ", "_")
    return f"{dataset_name}/{safe}/{month:%Y-%m}.csv.gz"


def _write_atomically(root: Path, relative: str, payload: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, target)


# --- checking ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayCheck:
    bars: int
    duplicates: int
    bad_ohlc: int
    zero_volume: int
    outside_session: int


def _in_session(at: datetime) -> bool:
    local = at.astimezone(MARKET_TIMEZONE)
    minutes = local.hour * 60 + local.minute
    return SESSION_OPEN[0] * 60 + SESSION_OPEN[1] <= minutes < SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]


def check_day(bars: list[Bar], *, daily: bool = False) -> DayCheck:
    counts = Counter(bar.at for bar in bars)
    bad = sum(
        1
        for bar in bars
        if min(bar.open, bar.high, bar.low, bar.close) <= 0
        or bar.high < max(bar.open, bar.close, bar.low)
        or bar.low > min(bar.open, bar.close, bar.high)
    )
    return DayCheck(
        bars=len(counts),
        duplicates=sum(n - 1 for n in counts.values() if n > 1),
        bad_ohlc=bad,
        zero_volume=sum(1 for bar in bars if bar.volume <= 0),
        outside_session=0 if daily else sum(1 for bar in bars if not _in_session(bar.at)),
    )


def by_session(bars: Iterable[Bar]) -> dict[date, list[Bar]]:
    days: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        days[bar.at.astimezone(MARKET_TIMEZONE).date()].append(bar)
    return days


# --- datasets ------------------------------------------------------------------------------


async def open_dataset(
    session: AsyncSession,
    *,
    name: str,
    timeframe: str,
    start: date,
    end: date,
    benchmark_key: str = NIFTY_50,
    source: str = UPSTOX_PUBLIC_SOURCE,
    provenance: dict | None = None,
) -> ResearchDataset:
    """The named dataset, created if new. A finished dataset is never reopened for writing."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe {timeframe!r}; use one of {', '.join(TIMEFRAMES)}")
    dataset = await session.scalar(select(ResearchDataset).where(ResearchDataset.name == name))
    if dataset is not None:
        if dataset.status == "COMPLETE":
            raise ValueError(f"Dataset {name!r} is complete and immutable; build a new one under another name")
        if (dataset.timeframe, dataset.start_date, dataset.end_date) != (timeframe, start, end):
            raise ValueError(f"Dataset {name!r} exists with a different timeframe or date range")
        return dataset
    dataset = ResearchDataset(
        name=name,
        source=source,
        timeframe=timeframe,
        price_adjustment=ADJUSTED_BY_PROVIDER if source == UPSTOX_PUBLIC_SOURCE else "UNKNOWN",
        benchmark_key=benchmark_key,
        start_date=start,
        end_date=end,
        status="BUILDING",
        storage_path=name,
        provenance={
            "source_url_pattern": UPSTOX_HISTORY_URL if source == UPSTOX_PUBLIC_SOURCE else None,
            "credentials": "none",
            **(provenance or {}),
        },
    )
    session.add(dataset)
    await session.flush()
    return dataset


@dataclass
class BackfillReport:
    files_written: int = 0
    files_kept: int = 0
    bars_written: int = 0
    empty_months: int = 0
    requests: int = 0
    stopped_early: str | None = None


async def backfill(
    session: AsyncSession,
    dataset: ResearchDataset,
    instrument_keys: list[str],
    source,  # noqa: ANN001 - anything with fetch(); UpstoxPublicHistory in production
    *,
    root: Path | None = None,
    today: date | None = None,
    keep_going: Callable[[], bool] = lambda: True,
) -> BackfillReport:
    """Fetch every month of every instrument, benchmark first. Resumable; commits each file.

    A month already on disk with the recorded hash is kept, unless it is the
    current month or later, which may still be incomplete. ``keep_going`` is
    asked between files so a run can stop when the research window closes; the
    next run picks up where this one left off.
    """
    root = root or data_root()
    today = today or datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()
    current_month = date(today.year, today.month, 1)
    report = BackfillReport()
    keys = list(dict.fromkeys([dataset.benchmark_key, *instrument_keys]))
    existing = {
        (row.instrument_key, row.month): row
        for row in (
            await session.scalars(select(ResearchDatasetFile).where(ResearchDatasetFile.dataset_id == dataset.id))
        ).all()
    }
    for key in keys:
        for month, first, last in month_ranges(dataset.start_date, dataset.end_date):
            if not keep_going():
                report.stopped_early = "The research window closed; run the backfill again to continue."
                report.requests = getattr(source, "requests", 0)
                return report
            relative = file_path(dataset.name, key, month)
            known = existing.get((key, month))
            if known is not None and month < current_month and _file_matches(root / relative, known.sha256):
                report.files_kept += 1
                continue
            bars = await source.fetch(key, dataset.timeframe, first, last)
            payload = encode_bars(bars)
            digest = hashlib.sha256(payload).hexdigest()
            _write_atomically(root, relative, payload)
            if known is None:
                known = ResearchDatasetFile(dataset_id=dataset.id, instrument_key=key, month=month)
                session.add(known)
                existing[(key, month)] = known
            known.relative_path = relative
            known.rows = len(bars)
            known.sha256 = digest
            known.first_bar_at = bars[0].at if bars else None
            known.last_bar_at = bars[-1].at if bars else None
            known.fetched_at = datetime.now(UTC)
            await session.commit()
            report.files_written += 1
            report.bars_written += len(bars)
            report.empty_months += not bars
    report.requests = getattr(source, "requests", 0)
    return report


def _file_matches(path: Path, sha256: str) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    except OSError:
        return False


async def _files(session: AsyncSession, dataset_id: UUID) -> list[ResearchDatasetFile]:
    return list(
        (
            await session.scalars(
                select(ResearchDatasetFile)
                .where(ResearchDatasetFile.dataset_id == dataset_id)
                .order_by(ResearchDatasetFile.instrument_key, ResearchDatasetFile.month)
            )
        ).all()
    )


def _load(root: Path, row: ResearchDatasetFile) -> list[Bar]:
    payload = (root / row.relative_path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != row.sha256:
        raise ValueError(
            f"{row.relative_path} does not match its recorded SHA-256; the file changed after it was written"
        )
    return decode_bars(payload)


def _files_by_key(files: list[ResearchDatasetFile]) -> dict[str, list[ResearchDatasetFile]]:
    grouped: dict[str, list[ResearchDatasetFile]] = defaultdict(list)
    for row in files:
        grouped[row.instrument_key].append(row)
    return grouped


def _day_checks(root: Path, rows: list[ResearchDatasetFile], *, daily: bool) -> dict[date, DayCheck]:
    """One instrument's per-session checks, one monthly file in memory at a time.

    A session's bars all sit in the file of its month (files are cut by local
    date), so checking file by file gives the same answer as the whole series.
    """
    checks: dict[date, DayCheck] = {}
    for row in rows:
        for day, bars in by_session(_load(root, row)).items():
            checks[day] = check_day(bars, daily=daily)
    return checks


async def compute_coverage(session: AsyncSession, dataset: ResearchDataset, *, root: Path | None = None) -> int:
    """Rebuild the dataset's coverage rows. Returns how many were written.

    One instrument at a time, written with a bulk insert: every bar of a
    200-share minute dataset (tens of millions), or every coverage row as an
    ORM object (a quarter of a million), does not fit in the research worker.
    """
    from sqlalchemy import insert

    root = root or data_root()
    daily = dataset.timeframe == "day"
    grouped = _files_by_key(await _files(session, dataset.id))
    benchmark_days = {
        day: check.bars for day, check in _day_checks(root, grouped.get(dataset.benchmark_key, []), daily=daily).items()
    }

    await session.execute(delete(ResearchDatasetCoverage).where(ResearchDatasetCoverage.dataset_id == dataset.id))
    written = 0
    empty = DayCheck(0, 0, 0, 0, 0)
    for key, rows in grouped.items():
        checks = _day_checks(root, rows, daily=daily)
        values = []
        for day in sorted(set(checks) | set(benchmark_days)):
            check = checks.get(day, empty)
            expected = benchmark_days.get(day, 0)
            values.append(
                {
                    "dataset_id": dataset.id,
                    "instrument_key": key,
                    "session_date": day,
                    "bars": check.bars,
                    "expected_bars": expected,
                    "missing_bars": max(0, expected - check.bars),
                    "duplicates": check.duplicates,
                    "bad_ohlc": check.bad_ohlc,
                    "zero_volume": check.zero_volume,
                    "outside_session": check.outside_session,
                }
            )
        if values:
            await session.execute(insert(ResearchDatasetCoverage), values)
            written += len(values)
    await session.flush()
    return written


async def finalize(session: AsyncSession, dataset: ResearchDataset, *, root: Path | None = None) -> str:
    """Fix the dataset's content hash and mark it COMPLETE. Every file is re-hashed first."""
    root = root or data_root()
    files = await _files(session, dataset.id)
    lines = []
    for row in files:
        if not _file_matches(root / row.relative_path, row.sha256):
            raise ValueError(f"{row.relative_path} is missing or changed; the dataset cannot be finalised")
        lines.append(f"{row.instrument_key}|{row.month.isoformat()}|{row.sha256}")
    digest = hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()
    dataset.content_sha256 = digest
    dataset.instrument_count = len({row.instrument_key for row in files})
    dataset.row_count = sum(row.rows for row in files)
    dataset.status = "COMPLETE"
    dataset.finalized_at = datetime.now(UTC)
    await session.flush()
    return digest


# A loaded CompletedCandle of minute data, measured: about 712 bytes with its
# Decimals. The research worker has 1 GB, shared with whatever else it runs.
CANDLE_BYTES = 712
READ_LIMIT = 500_000  # about 340 MB of candles


class DatasetTooLarge(ValueError):
    """The run would load more candles than the research worker can hold."""


def _month_overlaps(month: date, start: date, end: date) -> bool:
    return date(start.year, start.month, 1) <= month <= end


async def read_dataset(
    session: AsyncSession,
    dataset_id: UUID,
    instrument_keys: list[str],
    start: date,
    end: date,
    *,
    root: Path | None = None,
    max_candles: int | None = READ_LIMIT,
) -> tuple[ResearchDataset, dict[str, list[CompletedCandle]], list[CompletedCandle]]:
    """A dataset's bars as completed candles, for the backtester. Repeated timestamps are dropped.

    Everything is held in memory at once, so a run larger than ``max_candles``
    is refused before anything is read (``DatasetTooLarge``).
    """
    root = root or data_root()
    dataset = await session.get(ResearchDataset, dataset_id)
    if dataset is None:
        raise ValueError("Dataset not found")
    grouped = _files_by_key(await _files(session, dataset.id))
    wanted = set(instrument_keys) | {dataset.benchmark_key}
    if max_candles is not None:
        planned = sum(
            row.rows for key in wanted for row in grouped.get(key, []) if _month_overlaps(row.month, start, end)
        )
        if planned > max_candles:
            raise DatasetTooLarge(
                f"This range holds about {planned:,} candles; one run may load at most {max_candles:,} "
                f"(roughly {max_candles * CANDLE_BYTES // 2**20} MB). Use fewer shares or a shorter range, "
                "or a pre-registered experiment, which replays a month at a time."
            )
    candles = {key: window(root, dataset, grouped.get(key, []), start, end) for key in sorted(wanted)}
    benchmark = candles.pop(dataset.benchmark_key, [])
    return dataset, candles, benchmark


def window(
    root: Path, dataset: ResearchDataset, rows: list[ResearchDatasetFile], start: date, end: date
) -> list[CompletedCandle]:
    """One instrument's bars from ``start`` to ``end`` as completed candles, oldest first.

    Only the monthly files that overlap the window are read. Repeated
    timestamps are dropped.
    """
    seconds = TIMEFRAMES[dataset.timeframe][2]
    candles: list[CompletedCandle] = []
    for row in sorted(rows, key=lambda item: item.month):
        if not _month_overlaps(row.month, start, end):
            continue
        seen: set[datetime] = set()
        for bar in _load(root, row):
            local_day = bar.at.astimezone(MARKET_TIMEZONE).date()
            if bar.at in seen or not (start <= local_day <= end):
                continue
            seen.add(bar.at)
            opened = bar.at.astimezone(UTC)
            candles.append(
                CompletedCandle(
                    instrument_token=row.instrument_key,
                    timeframe_seconds=seconds,
                    opened_at=opened,
                    closed_at=opened + timedelta(seconds=seconds),
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    tick_count=0,
                )
            )
    candles.sort(key=lambda candle: candle.opened_at)
    return candles


async def benchmark_days(
    session: AsyncSession, dataset: ResearchDataset, start: date, end: date, *, root: Path | None = None
) -> dict[date, str]:
    """Each benchmark session from ``start`` to ``end``: "UP" or "DOWN", close against open.

    Read a month at a time, so the session list and the regime of a long
    period never need the whole benchmark series in memory.
    """
    root = root or data_root()
    rows = (_files_by_key(await _files(session, dataset.id))).get(dataset.benchmark_key, [])
    days: dict[date, str] = {}
    for row in sorted(rows, key=lambda item: item.month):
        if not _month_overlaps(row.month, start, end):
            continue
        for day, bars in by_session(_load(root, row)).items():
            if start <= day <= end and bars:
                ordered = sorted(bars, key=lambda bar: bar.at)
                days[day] = "UP" if ordered[-1].close >= ordered[0].open else "DOWN"
    return dict(sorted(days.items()))


# --- the calendar a historical replay needs ----------------------------------------------


class DatasetCalendar(TradingCalendar):
    """Trading days are the days the dataset's benchmark traded; regular hours on each.

    The application calendar knows only the years an operator confirmed (2026),
    and ``market_calculations.session_candles`` keeps only candles that calendar
    calls open -- so a replay of 2022 bars computed no EMA, no ATR and no
    relative strength, and every strategy refused every candle as "indicators
    not ready". Nothing failed; the backtest simply never traded. Found on the
    first real-data run of Phase 4.

    The benchmark's own bars are the evidence a day was a session, the same
    rule coverage uses. Hours are the regular 09:15-15:30.
    """

    # Answers remembered at most; a replay revisits the same minutes, never this many.
    REMEMBERED = 200_000

    def __init__(self, sessions: Iterable[date]) -> None:
        super().__init__()
        self._dataset_sessions = frozenset(sessions)
        self._open_at: dict[datetime, bool] = {}

    def is_open(self, timestamp: datetime) -> bool:
        """Remembered per instant: this calendar never changes during a replay.

        ``session_candles`` re-checks a session's whole history for every new
        candle, so a 375-minute session asks about each minute ~190 times;
        that was 70% of a replay's time (profiled 28 Nov 2026). The answer
        depends on the instant alone.
        """
        known = self._open_at.get(timestamp)
        if known is None:
            if len(self._open_at) >= self.REMEMBERED:
                self._open_at.clear()
            known = super().is_open(timestamp)
            self._open_at[timestamp] = known
        return known

    def session_for(self, trading_date: date) -> TradingSession | None:
        return TradingSession(trading_date=trading_date) if trading_date in self._dataset_sessions else None

    def closed_reason(self, trading_date: date) -> str:
        if trading_date in self._dataset_sessions:
            return "Outside exchange session"
        return "No benchmark bars in this dataset on this day"


@contextmanager
def replay_calendar(sessions: Iterable[date]) -> Iterator[DatasetCalendar]:
    """Use the dataset's sessions as the calendar for indicator calculations, then restore.

    Process-local: it replaces ``market_calculations``' module default, which is
    only safe in a process that does nothing else -- the research worker's job
    process. Never call it in the API or the scanner.
    """
    from app.services import market_calculations

    calendar = DatasetCalendar(sessions)
    previous = market_calculations.DEFAULT_TRADING_CALENDAR
    market_calculations.DEFAULT_TRADING_CALENDAR = calendar
    try:
        yield calendar
    finally:
        market_calculations.DEFAULT_TRADING_CALENDAR = previous
