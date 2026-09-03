"""Dynamic scan-universe selection.

The ranking function is pure and deterministic: given the same daily candles and
controls it always produces the same ordered candidate list. The database refresh
wrapper reads persisted daily candles and the current session's first minute, ranks
the streamed instruments, and stores the result in ``scan_universe``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import delete, select

from app.core.config import Settings
from app.db.models import MarketCandle, ScanUniverseEntry
from app.db.session import SessionLocal
from app.services.candle_aggregation import DAILY_TIMEFRAME_SECONDS, _as_completed
from app.services.market_calculations import CompletedCandle, atr
from app.services.trading_calendar import MARKET_TIMEZONE
from app.services.upstox_market_data import configured_subscriptions

logger = structlog.get_logger("universe")

MIN_DAILY_CANDLES = 6


@dataclass(frozen=True)
class UniverseControls:
    size: int
    min_avg_turnover: Decimal
    min_price: Decimal
    max_price: Decimal
    min_atr_percent: Decimal
    max_atr_percent: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> UniverseControls:
        return cls(
            size=settings.universe_size,
            min_avg_turnover=Decimal(str(settings.universe_min_avg_turnover)),
            min_price=Decimal(str(settings.universe_min_price)),
            max_price=Decimal(str(settings.universe_max_price)),
            min_atr_percent=Decimal(str(settings.universe_min_atr_percent)),
            max_atr_percent=Decimal(str(settings.universe_max_atr_percent)),
        )


@dataclass(frozen=True)
class UniverseCandidate:
    instrument_token: str
    eligible: bool
    rejection_reason: str | None
    score: float
    rank: int
    selected: bool
    components: dict[str, float]
    metrics: dict[str, float]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _instrument_metrics(daily: list[CompletedCandle], session_open: Decimal | None) -> dict[str, float] | str:
    """Return raw metrics, or a rejection reason string when the instrument cannot be judged."""
    ordered = sorted(daily, key=lambda candle: candle.opened_at)
    if len(ordered) < MIN_DAILY_CANDLES:
        return "Insufficient daily history"
    prior_close = ordered[-1].close
    if prior_close <= 0:
        return "Invalid prior close"
    turnovers = [candle.close * candle.volume for candle in ordered[-20:]]
    avg_turnover = sum(turnovers, start=Decimal("0")) / Decimal(len(turnovers))
    daily_atr_value = atr(ordered, min(14, len(ordered) - 1))
    atr_percent = (daily_atr_value / prior_close * Decimal("100")) if daily_atr_value else Decimal("0")
    gap_percent = ((session_open - prior_close) / prior_close * Decimal("100")) if session_open else Decimal("0")
    lookback = ordered[-6].close if len(ordered) >= 6 else ordered[0].close
    trend_percent = ((prior_close / lookback) - Decimal("1")) * Decimal("100") if lookback > 0 else Decimal("0")
    return {
        "prior_close": float(prior_close),
        "avg_turnover": float(avg_turnover),
        "atr_percent": float(atr_percent),
        "gap_percent": float(gap_percent),
        "trend_percent": float(trend_percent),
        "daily_candles": float(len(ordered)),
    }


def _gate_reason(metrics: dict[str, float], controls: UniverseControls) -> str | None:
    if metrics["avg_turnover"] < float(controls.min_avg_turnover):
        return "Below liquidity floor"
    if not (float(controls.min_price) <= metrics["prior_close"] <= float(controls.max_price)):
        return "Outside price band"
    if not (float(controls.min_atr_percent) <= metrics["atr_percent"] <= float(controls.max_atr_percent)):
        return "Outside volatility band"
    return None


def _gap_score(abs_gap: float) -> float:
    if abs_gap <= 0.3:
        return 60.0
    if abs_gap <= 2.5:
        return 100.0
    if abs_gap <= 5.0:
        return _clamp(100.0 - (abs_gap - 2.5) * 28.0)
    return 20.0


def rank_universe(
    daily_by_instrument: dict[str, list[CompletedCandle]],
    session_open_by_instrument: dict[str, Decimal] | None,
    controls: UniverseControls,
) -> list[UniverseCandidate]:
    """Rank streamed instruments by a liquidity / volatility / gap / momentum composite."""
    session_opens = session_open_by_instrument or {}
    raw: dict[str, dict[str, float]] = {}
    rejected: dict[str, str] = {}
    for token, daily in daily_by_instrument.items():
        result = _instrument_metrics(daily, session_opens.get(token))
        if isinstance(result, str):
            rejected[token] = result
            continue
        reason = _gate_reason(result, controls)
        if reason:
            rejected[token] = reason
            continue
        raw[token] = result

    turnovers = sorted(metrics["avg_turnover"] for metrics in raw.values())
    sweet_spot = (
        float(controls.min_atr_percent) + (float(controls.max_atr_percent) - float(controls.min_atr_percent)) * 0.35
    )
    band_half = max(float(controls.max_atr_percent) - sweet_spot, sweet_spot - float(controls.min_atr_percent), 0.1)

    scored: list[UniverseCandidate] = []
    for token, metrics in raw.items():
        percentile = (
            100.0 * (turnovers.index(metrics["avg_turnover"]) / (len(turnovers) - 1)) if len(turnovers) > 1 else 100.0
        )
        liquidity = _clamp(percentile)
        volatility = _clamp(100.0 * (1.0 - abs(metrics["atr_percent"] - sweet_spot) / band_half))
        gap = _gap_score(abs(metrics["gap_percent"]))
        trend = _clamp(40.0 + abs(metrics["trend_percent"]) * 6.0)
        composite = round(liquidity * 0.30 + volatility * 0.25 + gap * 0.25 + trend * 0.20, 2)
        scored.append(
            UniverseCandidate(
                instrument_token=token,
                eligible=True,
                rejection_reason=None,
                score=composite,
                rank=0,
                selected=False,
                components={
                    "liquidity": round(liquidity, 2),
                    "volatility": round(volatility, 2),
                    "gap": round(gap, 2),
                    "trend": round(trend, 2),
                },
                metrics={key: round(value, 4) for key, value in metrics.items()},
            )
        )

    scored.sort(key=lambda candidate: (-candidate.score, candidate.instrument_token))
    ranked = [
        UniverseCandidate(
            instrument_token=candidate.instrument_token,
            eligible=True,
            rejection_reason=None,
            score=candidate.score,
            rank=index + 1,
            selected=index < controls.size,
            components=candidate.components,
            metrics=candidate.metrics,
        )
        for index, candidate in enumerate(scored)
    ]
    offset = len(ranked)
    for order, (token, reason) in enumerate(sorted(rejected.items())):
        ranked.append(
            UniverseCandidate(
                instrument_token=token,
                eligible=False,
                rejection_reason=reason,
                score=0.0,
                rank=offset + order + 1,
                selected=False,
                components={"liquidity": 0.0, "volatility": 0.0, "gap": 0.0, "trend": 0.0},
                metrics={},
            )
        )
    return ranked


async def _daily_candles(session, token: str, limit: int) -> list[CompletedCandle]:
    rows = await session.scalars(
        select(MarketCandle)
        .where(MarketCandle.instrument_token == token, MarketCandle.timeframe_seconds == DAILY_TIMEFRAME_SECONDS)
        .order_by(MarketCandle.opened_at.desc())
        .limit(limit)
    )
    return [_as_completed(row) for row in rows.all()]


async def _session_open(session, token: str, session_date: date) -> Decimal | None:
    row = await session.scalar(
        select(MarketCandle)
        .where(
            MarketCandle.instrument_token == token,
            MarketCandle.timeframe_seconds == 60,
            MarketCandle.session_date == session_date,
        )
        .order_by(MarketCandle.opened_at.asc())
        .limit(1)
    )
    return row.open if row else None


async def refresh_universe(settings: Settings, session_date: date | None = None) -> dict:
    """Rebuild ``scan_universe`` for the given session from persisted daily candles."""
    target = session_date or datetime.now(MARKET_TIMEZONE).date()
    controls = UniverseControls.from_settings(settings)
    benchmark = settings.upstox_nifty_benchmark_key
    candidates = [token for token in configured_subscriptions(settings) if token != benchmark]
    if not candidates:
        logger.warning("universe.refresh_skipped_no_candidates")
        return {"session_date": target.isoformat(), "candidates": 0, "selected": 0, "eligible": 0}

    async with SessionLocal() as session:
        daily_by_instrument = {
            token: await _daily_candles(session, token, settings.daily_history_sessions) for token in candidates
        }
        session_open_by_instrument = {token: await _session_open(session, token, target) for token in candidates}
        ranked = rank_universe(daily_by_instrument, session_open_by_instrument, controls)

        await session.execute(delete(ScanUniverseEntry).where(ScanUniverseEntry.session_date == target))
        session.add_all(
            ScanUniverseEntry(
                session_date=target,
                instrument_token=candidate.instrument_token,
                rank=candidate.rank,
                score=Decimal(str(candidate.score)),
                selected=candidate.selected,
                eligible=candidate.eligible,
                rejection_reason=candidate.rejection_reason,
                liquidity_score=Decimal(str(candidate.components["liquidity"])),
                volatility_score=Decimal(str(candidate.components["volatility"])),
                gap_score=Decimal(str(candidate.components["gap"])),
                trend_score=Decimal(str(candidate.components["trend"])),
                metrics=candidate.metrics,
            )
            for candidate in ranked
        )
        await session.commit()

    selected = sum(candidate.selected for candidate in ranked)
    eligible = sum(candidate.eligible for candidate in ranked)
    logger.info(
        "universe.refreshed",
        session_date=target.isoformat(),
        candidates=len(candidates),
        eligible=eligible,
        selected=selected,
    )
    return {
        "session_date": target.isoformat(),
        "candidates": len(candidates),
        "eligible": eligible,
        "selected": selected,
        "built_at": datetime.now(UTC).isoformat(),
    }
