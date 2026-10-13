"""Intraday historical candle backfill for Upstox market data.

Fetches today's completed 1-minute candles from market open (09:15 IST) up to current time
to establish the 15-minute Opening Range (ORB) and session indicators immediately.

``backfill_intraday_range`` is the research counterpart: whole past sessions,
over a date range, for the backtester to replay. It deliberately does **not**
go through the live persistence path -- see its own docstring.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import Settings
from app.db.models import MarketCandle
from app.db.session import SessionLocal
from app.services.candle_aggregation import MarketCalculationPersistenceService
from app.services.market_calculations import CompletedCandle, is_regular_market_timestamp
from app.services.trading_calendar import DEFAULT_TRADING_CALENDAR, TradingCalendar
from app.services.upstox_market_data import configured_subscriptions

logger = structlog.get_logger("upstox.backfill")
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/intraday/{instrument_key}/1minute"
UPSTOX_DAILY_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
UPSTOX_RANGE_URL = "https://api.upstox.com/v2/historical-candle/{instrument_key}/1minute/{to_date}/{from_date}"
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
DAILY_TIMEFRAME_SECONDS = 86_400

# Upstox serves 1-minute candles a window at a time rather than for any span
# asked for, so a long range is walked in pieces. Under the documented limit
# rather than at it: a window that is refused costs a whole month of a whole
# instrument, and the only cost of a smaller one is more requests.
RANGE_WINDOW_DAYS = 25
# Rows per INSERT. Six months of one instrument is about fifty thousand candles,
# and one statement each would be fifty thousand round trips.
INSERT_BATCH = 1_000
# Between requests. Upstox publishes per-second and per-minute ceilings and this
# job is the least urgent thing in the system -- there is nothing to win by
# being close to them.
REQUEST_PAUSE_SECONDS = 0.25


async def backfill_today_candles(
    settings: Settings,
    persistence: MarketCalculationPersistenceService,
    access_token: str | None = None,
    benchmark_key: str | None = None,
    calendar: TradingCalendar | None = None,
) -> int:
    """Fetch and persist today's historical 1-minute candles for all configured instruments."""
    token = access_token or settings.upstox_access_token
    if not token:
        logger.warning("upstox.backfill_skipped_no_token")
        return 0

    benchmark = benchmark_key or settings.upstox_nifty_benchmark_key
    instruments = [benchmark] + [k for k in configured_subscriptions(settings) if k != benchmark]
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    total_backfilled = 0
    today_date = datetime.now(MARKET_TIMEZONE).date()

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for instrument_key in instruments:
            url = UPSTOX_HISTORICAL_URL.format(instrument_key=instrument_key)
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(
                        "upstox.backfill_failed_status", instrument=instrument_key, status=response.status_code
                    )
                    continue

                data = response.json()
                raw_candles = data.get("data", {}).get("candles", [])
                if not raw_candles:
                    continue

                # Upstox returns candles in reverse chronological order: [latest, ..., earliest]
                # We need chronological order: earliest (09:15) -> latest
                sorted_candles = list(reversed(raw_candles))
                instrument_count = 0

                for item in sorted_candles:
                    # Item format: [timestamp_str, open, high, low, close, volume, open_interest]
                    if len(item) < 6:
                        continue
                    ts_str, open_p, high_p, low_p, close_p, vol = item[0], item[1], item[2], item[3], item[4], item[5]
                    try:
                        opened_at = datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue

                    # Filter for today's session within regular market hours
                    if opened_at.astimezone(MARKET_TIMEZONE).date() != today_date:
                        continue
                    if not is_regular_market_timestamp(opened_at, calendar or DEFAULT_TRADING_CALENDAR):
                        continue

                    candle = CompletedCandle(
                        instrument_token=instrument_key,
                        timeframe_seconds=settings.candle_timeframe_seconds,
                        opened_at=opened_at,
                        closed_at=opened_at + timedelta(seconds=settings.candle_timeframe_seconds),
                        open=Decimal(str(open_p)),
                        high=Decimal(str(high_p)),
                        low=Decimal(str(low_p)),
                        close=Decimal(str(close_p)),
                        volume=int(vol or 0),
                        tick_count=1,
                    )
                    # Backfill rebuilds indicators and quality history only. Strategy
                    # evaluation is reserved for candles completed by the live feed.
                    await persistence.persist_completed(candle, notify_snapshot=False)
                    instrument_count += 1

                total_backfilled += instrument_count
                logger.info("upstox.instrument_backfilled", instrument=instrument_key, candles_count=instrument_count)

            except Exception as exc:
                logger.warning("upstox.backfill_instrument_error", instrument=instrument_key, error=str(exc))

    logger.info("upstox.backfill_completed", total_candles=total_backfilled)
    return total_backfilled


async def backfill_daily_history(
    settings: Settings,
    persistence: MarketCalculationPersistenceService,
    access_token: str | None = None,
    benchmark_key: str | None = None,
) -> int:
    """Fetch recent completed daily candles so prior-day levels, gaps and daily ATR are available.

    Stored in ``market_candles`` with ``timeframe_seconds = 86400``; the live 1-minute pipeline
    never reads that timeframe, so the two histories stay isolated.
    """
    token = access_token or settings.upstox_access_token
    if not token:
        logger.warning("upstox.daily_backfill_skipped_no_token")
        return 0

    benchmark = benchmark_key or settings.upstox_nifty_benchmark_key
    instruments = [benchmark] + [k for k in configured_subscriptions(settings) if k != benchmark]
    today_ist = datetime.now(MARKET_TIMEZONE).date()
    to_date = today_ist - timedelta(days=1)
    from_date = today_ist - timedelta(days=max(settings.daily_history_sessions, 5) * 2 + 10)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    total = 0
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for instrument_key in instruments:
            url = UPSTOX_DAILY_HISTORICAL_URL.format(
                instrument_key=instrument_key,
                to_date=to_date.isoformat(),
                from_date=from_date.isoformat(),
            )
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(
                        "upstox.daily_backfill_failed_status", instrument=instrument_key, status=response.status_code
                    )
                    continue
                raw_candles = response.json().get("data", {}).get("candles", [])
                sorted_candles = sorted(raw_candles, key=lambda item: item[0])[-settings.daily_history_sessions :]
                stored = 0
                for item in sorted_candles:
                    if len(item) < 6:
                        continue
                    try:
                        session_date = date.fromisoformat(str(item[0])[:10])
                    except ValueError:
                        continue
                    opened_at = datetime.combine(session_date, time(9, 15), tzinfo=MARKET_TIMEZONE)
                    candle = CompletedCandle(
                        instrument_token=instrument_key,
                        timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
                        opened_at=opened_at,
                        closed_at=datetime.combine(session_date, time(15, 30), tzinfo=MARKET_TIMEZONE),
                        open=Decimal(str(item[1])),
                        high=Decimal(str(item[2])),
                        low=Decimal(str(item[3])),
                        close=Decimal(str(item[4])),
                        volume=int(item[5] or 0),
                        tick_count=1,
                    )
                    if await persistence.persist_daily_candle(candle):
                        stored += 1
                total += stored
                logger.info("upstox.daily_instrument_backfilled", instrument=instrument_key, sessions=stored)
            except Exception as exc:
                logger.warning("upstox.daily_backfill_instrument_error", instrument=instrument_key, error=str(exc))

    logger.info("upstox.daily_backfill_completed", total_sessions=total)
    return total


@dataclass
class RangeBackfillReport:
    """What a range backfill actually managed, per instrument.

    Returned rather than logged alone because the operator's question is never
    "did it run" but "what have I got now" -- which instrument is short, and
    whether a gap is Upstox's or ours.
    """

    from_date: date
    to_date: date
    stored: dict[str, int] = field(default_factory=dict)
    already_present: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, set[date]] = field(default_factory=dict)
    failures: dict[str, list[str]] = field(default_factory=dict)
    # Rows Upstox sent that we refused to store. Counted rather than ignored
    # because the calendar fails closed for a year it has not confirmed: ask
    # for a range reaching into one and every candle from it is dropped in
    # silence, leaving a backtest running on half the history it was told it
    # had. A number here that is not small is that, or a holiday list that
    # disagrees with the exchange.
    out_of_session: dict[str, int] = field(default_factory=dict)

    @property
    def total_stored(self) -> int:
        return sum(self.stored.values())

    def note_failure(self, instrument: str, detail: str) -> None:
        self.failures.setdefault(instrument, []).append(detail)


def _windows(from_date: date, to_date: date, size_days: int = RANGE_WINDOW_DAYS) -> list[tuple[date, date]]:
    """The range in request-sized pieces, oldest first."""
    if to_date < from_date:
        return []
    spans: list[tuple[date, date]] = []
    start = from_date
    while start <= to_date:
        end = min(start + timedelta(days=size_days - 1), to_date)
        spans.append((start, end))
        start = end + timedelta(days=1)
    return spans


def _parse_candle(
    item: list,
    instrument_key: str,
    timeframe_seconds: int,
    calendar: TradingCalendar,
) -> CompletedCandle | None:
    """One Upstox row as a candle, or None if it is not one we store.

    Rows outside regular market hours are dropped here rather than filtered
    later: pre-open and post-close prints would otherwise become candles the
    backtester replays as if they were tradeable.
    """
    if len(item) < 6:
        return None
    try:
        opened_at = datetime.fromisoformat(str(item[0]))
    except (ValueError, TypeError):
        return None
    if not is_regular_market_timestamp(opened_at, calendar):
        return None
    try:
        return CompletedCandle(
            instrument_token=instrument_key,
            timeframe_seconds=timeframe_seconds,
            opened_at=opened_at,
            closed_at=opened_at + timedelta(seconds=timeframe_seconds),
            open=Decimal(str(item[1])),
            high=Decimal(str(item[2])),
            low=Decimal(str(item[3])),
            close=Decimal(str(item[4])),
            volume=int(item[5] or 0),
            tick_count=1,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


async def _store_batch(candles: list[CompletedCandle]) -> int:
    """Insert what is not already there, and report how many were new.

    ``ON CONFLICT DO NOTHING`` against the bucket's unique constraint, so this
    whole job is idempotent: re-running a range that is already loaded stores
    nothing and raises nothing, which is what makes it safe to resume after a
    failed window rather than starting over.
    """
    if not candles:
        return 0
    rows = [
        {
            "instrument_token": candle.instrument_token,
            "timeframe_seconds": candle.timeframe_seconds,
            "session_date": candle.session_date,
            "opened_at": candle.opened_at,
            "closed_at": candle.closed_at,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "tick_count": candle.tick_count,
        }
        for candle in candles
    ]
    statement = (
        pg_insert(MarketCandle)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_market_candle_bucket")
        .returning(MarketCandle.id)
    )
    async with SessionLocal() as session:
        result = await session.execute(statement)
        inserted = len(result.fetchall())
        await session.commit()
    return inserted


async def backfill_intraday_range(
    settings: Settings,
    *,
    from_date: date,
    to_date: date,
    access_token: str | None = None,
    benchmark_key: str | None = None,
    calendar: TradingCalendar | None = None,
    instruments: list[str] | None = None,
    window_days: int = RANGE_WINDOW_DAYS,
    pause_seconds: float = REQUEST_PAUSE_SECONDS,
) -> RangeBackfillReport:
    """Load whole past sessions of 1-minute candles, for research only.

    The backtester replays ``market_candles`` rows at the scanning timeframe.
    With four sessions stored it returns three trades, which is why no question
    about this system's settings has been answerable: a score threshold, a
    reward:risk, a holding limit and a share universe are all choices between
    distributions, and there has been no distribution to look at.

    **It does not go through the live persistence path, deliberately.**
    ``persist_completed`` recomputes indicators, rewrites data-quality history
    and can publish a snapshot; running it over hundreds of thousands of
    historical candles would overwrite the state the live scanner reads and
    bury the database in work during a session. Nothing here evaluates a
    strategy either, so no historical candle can produce a signal, an order or
    an alert. This writes raw OHLCV to ``market_candles`` and touches nothing
    else.

    Idempotent per candle, so a run that dies halfway can simply be repeated,
    and a range that overlaps what is already stored costs only the fetching.
    """
    token = access_token or settings.upstox_access_token
    if not token:
        logger.warning("upstox.range_backfill_skipped_no_token")
        return RangeBackfillReport(from_date=from_date, to_date=to_date)

    active_calendar = calendar or DEFAULT_TRADING_CALENDAR
    benchmark = benchmark_key or settings.upstox_nifty_benchmark_key
    targets = instruments or ([benchmark] + [k for k in configured_subscriptions(settings) if k != benchmark])
    timeframe = settings.candle_timeframe_seconds
    report = RangeBackfillReport(from_date=from_date, to_date=to_date)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        for instrument_key in targets:
            report.stored.setdefault(instrument_key, 0)
            report.sessions.setdefault(instrument_key, set())
            report.out_of_session.setdefault(instrument_key, 0)
            pending: list[CompletedCandle] = []
            seen = 0

            for window_from, window_to in _windows(from_date, to_date, window_days):
                url = UPSTOX_RANGE_URL.format(
                    instrument_key=instrument_key,
                    to_date=window_to.isoformat(),
                    from_date=window_from.isoformat(),
                )
                try:
                    response = await client.get(url)
                except Exception as exc:  # noqa: BLE001 - one window must not end the run
                    report.note_failure(instrument_key, f"{window_from}..{window_to}: {exc}")
                    continue
                if response.status_code != 200:
                    report.note_failure(instrument_key, f"{window_from}..{window_to}: HTTP {response.status_code}")
                    logger.warning(
                        "upstox.range_backfill_window_failed",
                        instrument=instrument_key,
                        window_from=window_from.isoformat(),
                        window_to=window_to.isoformat(),
                        status=response.status_code,
                    )
                    await asyncio.sleep(pause_seconds)
                    continue

                raw = response.json().get("data", {}).get("candles", []) or []
                for item in raw:
                    candle = _parse_candle(item, instrument_key, timeframe, active_calendar)
                    if candle is None:
                        report.out_of_session[instrument_key] += 1
                        continue
                    seen += 1
                    report.sessions[instrument_key].add(candle.session_date)
                    pending.append(candle)
                    if len(pending) >= INSERT_BATCH:
                        report.stored[instrument_key] += await _store_batch(pending)
                        pending = []
                await asyncio.sleep(pause_seconds)

            if pending:
                report.stored[instrument_key] += await _store_batch(pending)
            report.already_present[instrument_key] = seen - report.stored[instrument_key]
            logger.info(
                "upstox.range_instrument_backfilled",
                instrument=instrument_key,
                stored=report.stored[instrument_key],
                already_present=report.already_present[instrument_key],
                out_of_session=report.out_of_session[instrument_key],
                sessions=len(report.sessions[instrument_key]),
            )

    logger.info(
        "upstox.range_backfill_completed",
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        instruments=len(targets),
        total_stored=report.total_stored,
        instruments_with_failures=len(report.failures),
    )
    return report
