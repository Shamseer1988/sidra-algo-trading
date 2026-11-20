"""Daily price history for the swing desk: fetch it, read it, adjust it, check it.

Phase 1 of the swing desk. Nothing here trades, and nothing here is read by the
intraday desk:

* Daily candles live in ``market_candles`` at ``timeframe_seconds = 86400``,
  the same rows the intraday indicators already keep 40 sessions of. Every
  intraday reader asks for the latest N by date, so older history added here is
  invisible to it; the insert skips any day already stored, so the intraday
  rows are never rewritten.
* The intraday dynamic universe chooses from the configured watchlist only, so
  daily history for the NIFTY 200 does not change what intraday scans.

**Adjustment happens on read, from confirmed events only.** A split or bonus
shows in unadjusted history as a crash that never happened. Whether Upstox's
daily history is already adjusted is something the data has to answer, so
``suspected_splits`` flags overnight jumps matching a standard ratio, a person
confirms them in ``corporate_events``, and ``adjust_for_events`` applies only
those. A guessed adjustment applied silently would be worse than none.

**Coverage is measured against NIFTY, not the calendar.** The trading calendar
refuses years it has not been told are confirmed, which is right for deciding
whether to trade today and wrong for counting history: two years back it would
call every day closed. The index trades every session, so the days it has a
candle are the days a share should have one too.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CorporateEvent, MarketCandle
from app.services.candle_aggregation import _as_completed
from app.services.market_calculations import CompletedCandle
from app.services.upstox_backfill import (
    DAILY_TIMEFRAME_SECONDS,
    MARKET_TIMEZONE,
    UPSTOX_DAILY_HISTORICAL_URL,
    RangeBackfillReport,
    _store_batch,
)

logger = structlog.get_logger("swing.data")

# One request per year of one share. Upstox serves daily candles over long
# spans; a year keeps each answer small and a refused window cheap.
DAILY_WINDOW_DAYS = 365
REQUEST_PAUSE_SECONDS = 0.25

# Standard split and bonus price factors: 1:2 and 1:5 and 1:10 splits, 1:1 and
# 1:2 and 2:1 bonuses, and the rest seen on NSE. A jump within the tolerance of
# one of these is suspicious; anything else is just a big day.
STANDARD_FACTORS = (
    Decimal("1.25"),
    Decimal("1.5"),
    Decimal("2"),
    Decimal("3"),
    Decimal("4"),
    Decimal("5"),
    Decimal("10"),
)
FACTOR_TOLERANCE = Decimal("0.06")
# A real day rarely opens a third away from the last close; a split always does.
MIN_SUSPICIOUS_JUMP = Decimal("1.2")

ADJUSTING_EVENTS = ("SPLIT", "BONUS")


def parse_daily_row(item: list, instrument_token: str) -> CompletedCandle | None:
    """One Upstox daily row as a candle, stamped 09:15-15:30 IST like the intraday side's.

    The same opening time the existing daily backfill writes, so a day already
    stored by it is recognised by the unique key and left alone.
    """
    if len(item) < 6:
        return None
    try:
        session_date = date.fromisoformat(str(item[0])[:10])
        return CompletedCandle(
            instrument_token=instrument_token,
            timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
            opened_at=datetime.combine(session_date, time(9, 15), tzinfo=MARKET_TIMEZONE),
            closed_at=datetime.combine(session_date, time(15, 30), tzinfo=MARKET_TIMEZONE),
            open=Decimal(str(item[1])),
            high=Decimal(str(item[2])),
            low=Decimal(str(item[3])),
            close=Decimal(str(item[4])),
            volume=int(item[5] or 0),
            tick_count=1,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def windows(from_date: date, to_date: date, size_days: int = DAILY_WINDOW_DAYS) -> list[tuple[date, date]]:
    """The range in request-sized pieces, oldest first."""
    spans: list[tuple[date, date]] = []
    start = from_date
    while start <= to_date:
        end = min(start + timedelta(days=size_days - 1), to_date)
        spans.append((start, end))
        start = end + timedelta(days=1)
    return spans


async def backfill_daily_range(
    tokens: Iterable[str],
    from_date: date,
    to_date: date,
    *,
    access_token: str,
    client: httpx.AsyncClient | None = None,
    pause_seconds: float = REQUEST_PAUSE_SECONDS,
) -> RangeBackfillReport:
    """Fetch and store daily candles for ``tokens`` over the range. Idempotent.

    A failed window is recorded and skipped, never retried in a loop: re-running
    the job stores only what is missing, which is the recovery.
    """
    report = RangeBackfillReport(from_date=from_date, to_date=to_date)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0, headers=headers)
    try:
        for token in tokens:
            stored = 0
            for start, end in windows(from_date, to_date):
                url = UPSTOX_DAILY_HISTORICAL_URL.format(
                    instrument_key=token, to_date=end.isoformat(), from_date=start.isoformat()
                )
                try:
                    response = await http.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    report.note_failure(token, f"{start}..{end}: {type(exc).__name__}")
                    continue
                if response.status_code != 200:
                    report.note_failure(token, f"{start}..{end}: HTTP {response.status_code}")
                    continue
                rows = (response.json().get("data") or {}).get("candles") or []
                candles = [candle for candle in (parse_daily_row(row, token) for row in rows) if candle is not None]
                stored += await _store_batch(candles)
                report.sessions.setdefault(token, set()).update(candle.session_date for candle in candles)
                if pause_seconds:
                    await asyncio.sleep(pause_seconds)
            report.stored[token] = stored
            logger.info("swing.daily_backfilled", instrument=token, stored=stored)
    finally:
        if own_client:
            await http.aclose()
    return report


async def load_daily(
    session: AsyncSession, token: str, *, from_date: date | None = None, to_date: date | None = None
) -> list[CompletedCandle]:
    """Stored daily candles for one share, oldest first, as the broker sent them."""
    query = select(MarketCandle).where(
        MarketCandle.instrument_token == token, MarketCandle.timeframe_seconds == DAILY_TIMEFRAME_SECONDS
    )
    if from_date is not None:
        query = query.where(MarketCandle.session_date >= from_date)
    if to_date is not None:
        query = query.where(MarketCandle.session_date <= to_date)
    rows = await session.scalars(query.order_by(MarketCandle.session_date.asc()))
    return [_as_completed(row) for row in rows.all()]


async def confirmed_adjustments(session: AsyncSession, token: str) -> list[CorporateEvent]:
    rows = await session.scalars(
        select(CorporateEvent).where(
            CorporateEvent.instrument_token == token,
            CorporateEvent.event_type.in_(ADJUSTING_EVENTS),
            CorporateEvent.confirmed.is_(True),
            CorporateEvent.price_factor.is_not(None),
        )
    )
    return list(rows.all())


async def load_adjusted_daily(
    session: AsyncSession, token: str, *, from_date: date | None = None, to_date: date | None = None
) -> list[CompletedCandle]:
    """Daily candles with confirmed splits and bonuses applied. What a swing strategy reads."""
    bars = await load_daily(session, token, from_date=from_date, to_date=to_date)
    return adjust_for_events(bars, await confirmed_adjustments(session, token))


def adjust_for_events(bars: list[CompletedCandle], events: Iterable) -> list[CompletedCandle]:  # noqa: ANN001
    """Divide every price before each event's ex-date by its factor, and multiply volume by it.

    Several events compound: a share split 1:2 and later given a 1:1 bonus has
    its oldest prices divided by four. Bars on or after an ex-date are untouched
    by that event, because they are already in the new share count.
    """
    factors = sorted(
        ((event.ex_date, Decimal(str(event.price_factor))) for event in events if event.price_factor),
        key=lambda pair: pair[0],
    )
    if not factors:
        return list(bars)
    adjusted: list[CompletedCandle] = []
    for bar in bars:
        factor = Decimal("1")
        for ex_date, event_factor in factors:
            if bar.session_date < ex_date:
                factor *= event_factor
        if factor == 1:
            adjusted.append(bar)
            continue
        adjusted.append(
            CompletedCandle(
                instrument_token=bar.instrument_token,
                timeframe_seconds=bar.timeframe_seconds,
                opened_at=bar.opened_at,
                closed_at=bar.closed_at,
                open=bar.open / factor,
                high=bar.high / factor,
                low=bar.low / factor,
                close=bar.close / factor,
                volume=int(bar.volume * factor),
                tick_count=bar.tick_count,
            )
        )
    return adjusted


@dataclass(frozen=True)
class SuspectedSplit:
    instrument_token: str
    ex_date: date
    previous_close: Decimal
    open: Decimal
    jump: Decimal
    nearest_factor: Decimal


def suspected_splits(bars: list[CompletedCandle]) -> list[SuspectedSplit]:
    """Overnight drops that match a standard split or bonus ratio.

    Only drops: a split lowers the price. The jump is previous close over the
    next open, and it must sit within ``FACTOR_TOLERANCE`` of a standard factor
    -- a 2.04x drop is a 1:2 split, a 1.7x drop is a disaster or a data error,
    and neither of those is for this function to fix.
    """
    found: list[SuspectedSplit] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        if not current.open or current.open <= 0:
            continue
        jump = previous.close / current.open
        if jump < MIN_SUSPICIOUS_JUMP:
            continue
        nearest = min(STANDARD_FACTORS, key=lambda factor: abs(jump - factor))
        if abs(jump - nearest) / nearest <= FACTOR_TOLERANCE:
            found.append(
                SuspectedSplit(
                    instrument_token=current.instrument_token,
                    ex_date=current.session_date,
                    previous_close=previous.close,
                    open=current.open,
                    jump=jump.quantize(Decimal("0.0001")),
                    nearest_factor=nearest,
                )
            )
    return found


@dataclass
class Coverage:
    instrument_token: str
    sessions: int = 0
    first: date | None = None
    last: date | None = None
    missing: list[date] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.sessions > 0 and not self.missing


def coverage(token: str, bars: list[CompletedCandle], reference_days: Iterable[date]) -> Coverage:
    """Which of the index's trading days, from this share's first candle on, it has no candle for.

    Counted from the share's own first day, because a share listed last year is
    not missing the year before it existed.
    """
    days = sorted({bar.session_date for bar in bars})
    result = Coverage(instrument_token=token, sessions=len(days))
    if not days:
        return result
    result.first, result.last = days[0], days[-1]
    have = set(days)
    result.missing = [day for day in sorted(set(reference_days)) if days[0] <= day <= days[-1] and day not in have]
    return result
