"""What the scanner did on one day, in numbers a person can read.

The scanner records every check of every share by every strategy: about fifty
thousand rows a session. The screen showed the newest 150 of them, which is
roughly the last minute. This answers the questions people actually open it
with: what was taken, what nearly was, what stopped the rest, and which shares
each strategy looked at hardest.

Read-only. Every query is one index scan over one session, and the summary is
held for half a minute so several open screens cost the database one read.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScannerEvaluation as Evaluation
from app.services.trading_symbols import resolve_script_names

# Written by the data-quality gate, not by a strategy.
GATE_ID = "data-quality"
# Checks made while the clock alone rules a trade out. They are most of the day
# and explain nothing about a setup, so they are counted once and kept apart.
OUTSIDE_PREFIX = "Outside "
REASONS = 12
NEAR = 15
CACHE_SECONDS = 30.0


async def latest_session(session: AsyncSession) -> date | None:
    return await session.scalar(select(func.max(Evaluation.session_date)))


async def recent_sessions(session: AsyncSession, limit: int = 10) -> list[date]:
    """The newest sessions with any check, found by stepping back through the index rather than grouping the table."""
    found: list[date] = []
    day = await latest_session(session)
    while day is not None and len(found) < limit:
        found.append(day)
        day = await session.scalar(select(func.max(Evaluation.session_date)).where(Evaluation.session_date < day))
    return found


def _moment(row) -> dict:  # noqa: ANN001
    return {
        "id": str(row.id),
        "instrument_token": row.instrument_token,
        "at": row.candle_opened_at,
        "strategy_name": row.strategy_name,
        "side": row.side,
        "score": row.score,
        "reason": row.reason,
    }


_MOMENT = (
    Evaluation.id,
    Evaluation.instrument_token,
    Evaluation.candle_opened_at,
    Evaluation.strategy_name,
    Evaluation.side,
    Evaluation.score,
    Evaluation.reason,
)


async def build(session: AsyncSession, day: date) -> dict:
    on_day = Evaluation.session_date == day
    groups = (
        await session.execute(
            select(
                Evaluation.strategy_id,
                Evaluation.strategy_name,
                Evaluation.status,
                Evaluation.reason,
                func.count().label("rows"),
                func.count().filter(Evaluation.score > 0).label("scored"),
                func.max(Evaluation.score).label("best"),
            )
            .where(on_day)
            .group_by(Evaluation.strategy_id, Evaluation.strategy_name, Evaluation.status, Evaluation.reason)
        )
    ).all()

    strategies: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    totals = {"checks": 0, "outside_hours": 0, "blocked_by_data": 0, "scored": 0, "accepted": 0}
    for row in groups:
        totals["checks"] += row.rows
        totals["scored"] += row.scored
        if row.status == "ACCEPTED":
            totals["accepted"] += row.rows
        if row.strategy_id == GATE_ID:
            totals["blocked_by_data"] += row.rows
            continue
        outside = row.reason.startswith(OUTSIDE_PREFIX)
        item = strategies.setdefault(
            row.strategy_id,
            {
                "strategy_id": row.strategy_id,
                "strategy_name": row.strategy_name,
                "checks": 0,
                "outside_hours": 0,
                "scored": 0,
                "accepted": 0,
                "best_score": 0,
            },
        )
        item["checks"] += row.rows
        item["scored"] += row.scored
        item["best_score"] = max(item["best_score"], row.best or 0)
        if row.status == "ACCEPTED":
            item["accepted"] += row.rows
        elif outside:
            item["outside_hours"] += row.rows
            totals["outside_hours"] += row.rows
        else:
            reasons[row.reason] = reasons.get(row.reason, 0) + row.rows

    accepted = (
        await session.execute(
            select(*_MOMENT).where(on_day, Evaluation.status == "ACCEPTED").order_by(Evaluation.candle_opened_at)
        )
    ).all()
    near = (
        await session.execute(
            select(*_MOMENT)
            .where(on_day, Evaluation.score > 0, Evaluation.status != "ACCEPTED")
            .order_by(Evaluation.score.desc(), Evaluation.candle_opened_at)
            .limit(NEAR)
        )
    ).all()
    per_share = (
        await session.execute(
            select(
                Evaluation.instrument_token,
                func.count().filter(Evaluation.status == "ACCEPTED").label("accepted"),
                func.count().filter(Evaluation.score > 0).label("scored"),
                func.count().filter(Evaluation.strategy_id == GATE_ID).label("blocked"),
                func.max(Evaluation.candle_opened_at).label("last_at"),
            )
            .where(on_day)
            .group_by(Evaluation.instrument_token)
        )
    ).all()
    best = {
        row.instrument_token: _moment(row)
        for row in (
            await session.execute(
                select(*_MOMENT)
                .where(on_day, Evaluation.score > 0)
                .distinct(Evaluation.instrument_token)
                .order_by(Evaluation.instrument_token, Evaluation.score.desc(), Evaluation.candle_opened_at)
            )
        ).all()
    }
    # The close on the day's last candle, for the shares that have that candle.
    last_at = max((row.last_at for row in per_share), default=None)
    closes = (
        {
            token: float(close)
            for token, close in (
                await session.execute(
                    select(Evaluation.instrument_token, func.max(Evaluation.candle_close))
                    .where(on_day, Evaluation.candle_opened_at == last_at)
                    .group_by(Evaluation.instrument_token)
                )
            ).all()
        }
        if last_at
        else {}
    )

    names = await resolve_script_names(session, {row.instrument_token for row in per_share})

    def named(item: dict) -> dict:
        return {**item, "script_name": names.get(item["instrument_token"], item["instrument_token"])}

    return {
        "session_date": day,
        "totals": {**totals, "shares": len(per_share), "last_candle": last_at},
        "strategies": sorted(strategies.values(), key=lambda item: (-item["accepted"], -item["scored"])),
        "reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda item: -item[1])[:REASONS]
        ],
        "signals": [named(_moment(row)) for row in accepted],
        "near": [named(_moment(row)) for row in near],
        "shares": sorted(
            (
                {
                    "instrument_token": row.instrument_token,
                    "script_name": names.get(row.instrument_token, row.instrument_token),
                    "last_close": closes.get(row.instrument_token),
                    "accepted": row.accepted,
                    "scored": row.scored,
                    "blocked": row.blocked,
                    "best": named(best[row.instrument_token]) if row.instrument_token in best else None,
                }
                for row in per_share
            ),
            key=lambda item: (-item["accepted"], -(item["best"]["score"] if item["best"] else 0), item["script_name"]),
        ),
    }


_cache: dict[date, tuple[float, dict]] = {}
_lock = asyncio.Lock()


async def cached(session: AsyncSession, day: date) -> dict:
    held = _cache.get(day)
    if held and time.monotonic() - held[0] < CACHE_SECONDS:
        return held[1]
    async with _lock:
        held = _cache.get(day)
        if held and time.monotonic() - held[0] < CACHE_SECONDS:
            return held[1]
        report = await build(session, day)
        if len(_cache) > 12:
            _cache.clear()
        _cache[day] = (time.monotonic(), report)
        return report


@dataclass(frozen=True)
class Filters:
    day: date
    status: str | None = None
    strategy_id: str | None = None
    quality: str | None = None
    search: str | None = None
    scored_only: bool = False
    hide_outside: bool = False
    by_score: bool = False


async def page(session: AsyncSession, filters: Filters, limit: int, offset: int) -> tuple[int, list[Evaluation]]:
    """One page of the day's checks, filtered and counted by the database."""
    clauses = [Evaluation.session_date == filters.day]
    if filters.status:
        clauses.append(Evaluation.status == filters.status)
    if filters.strategy_id:
        clauses.append(Evaluation.strategy_id == filters.strategy_id)
    if filters.quality:
        clauses.append(Evaluation.data_quality_state == filters.quality)
    if filters.scored_only:
        clauses.append(Evaluation.score > 0)
    if filters.hide_outside:
        clauses.append(Evaluation.reason.not_like(f"{OUTSIDE_PREFIX}%"))
    if filters.search:
        tokens = list(
            (
                await session.scalars(
                    select(Evaluation.instrument_token).where(Evaluation.session_date == filters.day).distinct()
                )
            ).all()
        )
        names = await resolve_script_names(session, tokens)
        wanted = filters.search.strip().lower()
        clauses.append(
            Evaluation.instrument_token.in_(
                [token for token in tokens if wanted in token.lower() or wanted in names.get(token, "").lower()]
            )
        )
    total = int(await session.scalar(select(func.count()).select_from(Evaluation).where(*clauses)) or 0)
    statement: Select = select(Evaluation).where(*clauses)
    order = (Evaluation.score.desc(),) if filters.by_score else ()
    rows = await session.scalars(
        # The id settles ties, so a row is never on two pages or on none.
        statement.order_by(*order, Evaluation.candle_opened_at.desc(), Evaluation.id).offset(offset).limit(limit)
    )
    return total, list(rows.all())
