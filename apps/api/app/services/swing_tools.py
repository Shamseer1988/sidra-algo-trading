"""The swing desk's maintenance jobs, run from the Swing screens instead of from a shell.

Three things that were scripts:

* **Coverage** (``scripts/swing_data_coverage.py``): how complete the daily
  history is, and which overnight drops look like an unrecorded split. Read-only.
* **Fetch daily candles** (``scripts/swing_backfill.py``): heavy -- about 200
  shares from the Upstox API. Refused on a weekday between 09:00 and 15:35 IST,
  when it would compete with the intraday scanner for the broker's rate limit
  and the database.
* **Backtest** (``scripts/run_swing_backtest.py``): the same ``run_backtest``
  with the charges from Settings > Charges, also refused during market hours.
  The result is kept, the last ``KEEP_RUNS`` of them, so it can be read later.

One job at a time. A job's progress is stored as a setting, so every screen and
every API process sees the same state; one left "running" by a restarted API is
treated as abandoned after ``STALE_AFTER``.

Nothing here can place, change or cancel an order. Fetching writes only daily
candles, never over a day already stored; a backtest writes only its result.
"""

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting
from app.db.session import SessionLocal
from app.services.market_calculations import MARKET_TIMEZONE

logger = structlog.get_logger("swing.tools")

JOBS_KEY = "swing_jobs"
RUNS_KEY = "swing_backtest_runs"
KEEP_RUNS = 10
STALE_AFTER = timedelta(hours=2)
MIN_SESSIONS = 250  # 200 sessions of warm-up for the 200-day average, and some to test
JOB_KINDS = ("backfill", "backtest")


class SwingToolError(RuntimeError):
    pass


def market_hours(now: datetime | None = None) -> bool:
    """A weekday between 09:00 and 15:35 IST: heavy swing jobs wait for the close."""
    current = (now or datetime.now(MARKET_TIMEZONE)).astimezone(MARKET_TIMEZONE)
    return current.weekday() < 5 and time(9, 0) <= current.time() <= time(15, 35)


MARKET_HOURS_REFUSAL = "Market hours: this job waits until after 3:35 PM IST, so it never slows the intraday scanner."


# --- job state ---------------------------------------------------------------------


async def _get(session: AsyncSession, key: str, default: Any) -> Any:
    row = await session.get(ApplicationSetting, key)
    return row.value if row is not None and row.value is not None else default


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(ApplicationSetting, key)
    if row is None:
        session.add(ApplicationSetting(key=key, value=value))
    else:
        row.value = value


def _running(job: dict | None, now: datetime) -> bool:
    if not job or job.get("status") != "running":
        return False
    started = datetime.fromisoformat(job["started_at"])
    return now - started < STALE_AFTER


async def jobs() -> dict[str, dict]:
    """Each kind's latest job, with an abandoned "running" one reported as such."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        stored = dict(await _get(session, JOBS_KEY, {}))
    for kind, job in stored.items():
        if job.get("status") == "running" and not _running(job, now):
            stored[kind] = {**job, "status": "failed", "detail": "Stopped without finishing (the server restarted)."}
    return stored


async def claim(kind: str, user_id: Any, params: dict) -> dict:
    """Mark ``kind`` as running, or refuse if any swing job already is. One job at a time."""
    if kind not in JOB_KINDS:
        raise SwingToolError(f"Unknown job: {kind}")
    if market_hours():
        raise SwingToolError(MARKET_HOURS_REFUSAL)
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        stored = dict(await _get(session, JOBS_KEY, {}))
        busy = [name for name, job in stored.items() if _running(job, now)]
        if busy:
            raise SwingToolError(f"A {busy[0]} is already running. Wait for it to finish.")
        job = {
            "id": uuid4().hex,
            "status": "running",
            "started_at": now.isoformat(),
            "finished_at": None,
            "detail": "Started.",
            "params": params,
            "user_id": str(user_id) if user_id else None,
        }
        stored[kind] = job
        await _put(session, JOBS_KEY, stored)
        await session.commit()
    return job


async def finish(kind: str, status: str, detail: str) -> None:
    async with SessionLocal() as session:
        stored = dict(await _get(session, JOBS_KEY, {}))
        job = dict(stored.get(kind) or {})
        job.update(status=status, detail=detail[:500], finished_at=datetime.now(UTC).isoformat())
        stored[kind] = job
        await _put(session, JOBS_KEY, stored)
        await session.commit()


# --- coverage (read-only) ----------------------------------------------------------


async def coverage_report(settings: Settings, years: int = 2) -> dict:
    from app.services.swing_data import confirmed_adjustments, coverage, load_daily, suspected_splits
    from app.services.swing_universe import nifty200

    to_date = datetime.now(MARKET_TIMEZONE).date()
    from_date = to_date - timedelta(days=365 * max(years, 1))
    members = nifty200()
    shares, suspects = [], []
    async with SessionLocal() as session:
        index = await load_daily(session, settings.upstox_nifty_benchmark_key, from_date=from_date)
        reference = [bar.session_date for bar in index]
        for member in members:
            bars = await load_daily(session, member.instrument_token, from_date=from_date)
            result = coverage(member.instrument_token, bars, reference)
            status = (
                "empty"
                if result.sessions == 0
                else "gaps"
                if result.missing
                else "short"
                if result.sessions < MIN_SESSIONS
                else "complete"
            )
            shares.append(
                {
                    "symbol": member.symbol,
                    "instrument_token": member.instrument_token,
                    "sessions": result.sessions,
                    "first": result.first,
                    "last": result.last,
                    "missing": len(result.missing),
                    "status": status,
                }
            )
            confirmed = {event.ex_date for event in await confirmed_adjustments(session, member.instrument_token)}
            for suspect in suspected_splits(bars):
                suspects.append(
                    {
                        "symbol": member.symbol,
                        "instrument_token": member.instrument_token,
                        "ex_date": suspect.ex_date,
                        "previous_close": float(suspect.previous_close),
                        "open": float(suspect.open),
                        "jump": float(suspect.jump),
                        "nearest_factor": float(suspect.nearest_factor),
                        "confirmed": suspect.ex_date in confirmed,
                    }
                )
    counts = Counter(share["status"] for share in shares)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "index_days": len(reference),
        "last_index_day": reference[-1] if reference else None,
        "minimum_sessions": MIN_SESSIONS,
        "counts": {status: counts.get(status, 0) for status in ("complete", "gaps", "short", "empty")},
        "shares": [share for share in shares if share["status"] != "complete"],
        "suspects": suspects,
    }


# --- fetch daily candles -------------------------------------------------------------


async def run_backfill(settings: Settings, years: int) -> None:
    """The background half of a fetch. ``claim('backfill', ...)`` must have succeeded."""
    from app.services.swing_data import backfill_daily_range
    from app.services.swing_universe import nifty200
    from app.services.upstox_oauth import load_access_token

    try:
        token = await load_access_token(settings)
        if not token:
            await finish("backfill", "failed", "No Upstox login today. Log in from Settings > Connections first.")
            return
        to_date = datetime.now(MARKET_TIMEZONE).date() - timedelta(days=1)
        from_date = to_date - timedelta(days=365 * max(years, 1))
        tokens = [settings.upstox_nifty_benchmark_key] + [member.instrument_token for member in nifty200()]
        report = await backfill_daily_range(tokens, from_date, to_date, access_token=token)
        failed = len(report.failures)
        detail = (
            f"{report.total_stored:,} new daily candles stored for {len(tokens)} instruments, {from_date} to {to_date}."
        )
        if failed:
            detail += f" {failed} instrument(s) had a failed window; fetch again to fill only what is missing."
        await finish("backfill", "failed" if failed and report.total_stored == 0 else "done", detail)
    except Exception as exc:  # noqa: BLE001 - recorded for the screen, never raised into the API
        logger.exception("swing.backfill_failed")
        await finish("backfill", "failed", f"{type(exc).__name__}: {exc}")


# --- backtest ------------------------------------------------------------------------


def _metrics_dict(m) -> dict:  # noqa: ANN001
    return {
        "trades": m.trades,
        "wins": m.wins,
        "win_rate": round(m.win_rate, 4),
        "net_pnl": float(m.net_pnl),
        "charges": float(m.charges),
        "per_trade": float(m.expectancy),
        "average_r": float(m.average_r),
        "profit_factor": round(m.profit_factor, 3) if m.profit_factor is not None else None,
        "max_drawdown": float(m.max_drawdown),
        "average_holding_days": round(m.average_holding_days, 1),
    }


def summarise_result(result, rules, card: dict, symbols: dict[str, str]) -> dict:  # noqa: ANN001
    """A backtest as the screen shows it: in-sample and out-of-sample, overall and per strategy."""
    from app.services.swing_backtest import metrics, split

    cut, inside, outside = split(result, 0.7)
    per_strategy = {
        name: {
            "in_sample": _metrics_dict(metrics([t for t in inside if t.setup.strategy == name])),
            "out_of_sample": _metrics_dict(metrics([t for t in outside if t.setup.strategy == name])),
        }
        for name in rules.strategies
    }
    trades = [
        {
            "symbol": symbols.get(t.token, t.token),
            "strategy": t.setup.strategy,
            "entry_date": t.entry_date,
            "entry_price": float(t.entry_price),
            "exit_date": t.exit_date,
            "exit_reason": t.exit_reason,
            "quantity": t.quantity,
            "net_pnl": float(t.net_pnl),
            "r": float(t.r_multiple),
            "out_of_sample": t.entry_date >= cut if cut else False,
        }
        for t in sorted(result.trades, key=lambda trade: trade.entry_date)
    ]
    return {
        "from_date": result.dates[0] if result.dates else None,
        "to_date": result.dates[-1] if result.dates else None,
        "out_of_sample_from": cut,
        "strategies": list(rules.strategies),
        "capital": float(rules.capital),
        "risk_per_trade": float(rules.risk_per_trade),
        "charges": card,
        "in_sample": _metrics_dict(metrics(inside)),
        "out_of_sample": _metrics_dict(metrics(outside)),
        "per_strategy": per_strategy,
        "exits": dict(Counter(t.exit_reason for t in result.trades).most_common()),
        "skipped": dict(sorted(result.skipped.items(), key=lambda kv: -kv[1])),
        "trades": trades[-300:],
    }


async def run_backtest_job(settings: Settings, years: int, strategies: list[str]) -> None:
    """The background half of a backtest. ``claim('backtest', ...)`` must have succeeded."""
    import asyncio

    from app.services.charges import swing_charges
    from app.services.swing_backtest import run_backtest
    from app.services.swing_data import load_adjusted_daily
    from app.services.swing_settings import swing_rules
    from app.services.swing_universe import nifty200

    try:
        to_date = datetime.now(MARKET_TIMEZONE).date()
        from_date = to_date - timedelta(days=365 * max(years, 1))
        members = nifty200()
        async with SessionLocal() as session:
            rules = await swing_rules(session, tuple(strategies))
            card = await swing_charges(session)
            benchmark = await load_adjusted_daily(session, settings.upstox_nifty_benchmark_key, from_date=from_date)
            universe = {
                member.instrument_token: await load_adjusted_daily(
                    session, member.instrument_token, from_date=from_date
                )
                for member in members
            }
        if len(benchmark) < MIN_SESSIONS:
            await finish(
                "backtest",
                "failed",
                f"Only {len(benchmark)} NIFTY daily candles are stored; fetch daily candles first (Swing > Data).",
            )
            return
        sectors = {member.instrument_token: member.industry for member in members}
        symbols = {member.instrument_token: member.symbol for member in members}
        # CPU-bound: off the event loop, so the API keeps answering meanwhile.
        result = await asyncio.to_thread(
            run_backtest, universe, sectors, benchmark, rules=rules, costs=card.cost_model()
        )
        summary = summarise_result(result, rules, card.model_dump(), symbols)
        summary.update(
            id=uuid4().hex,
            finished_at=datetime.now(UTC).isoformat(),
            years=years,
            shares_with_data=sum(1 for bars in universe.values() if bars),
        )
        async with SessionLocal() as session:
            runs = list(await _get(session, RUNS_KEY, []))
            runs.insert(0, _jsonable(summary))
            await _put(session, RUNS_KEY, runs[:KEEP_RUNS])
            await session.commit()
        out = summary["out_of_sample"]
        await finish(
            "backtest",
            "done",
            f"Out-of-sample: {out['trades']} trades, Rs {out['per_trade']:,.0f} a trade after charges.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("swing.backtest_failed")
        await finish("backtest", "failed", f"{type(exc).__name__}: {exc}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def backtest_runs() -> list[dict]:
    async with SessionLocal() as session:
        return list(await _get(session, RUNS_KEY, []))
