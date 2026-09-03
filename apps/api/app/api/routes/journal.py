"""Paper-journal analytics and export; never exposes broker positions or orders."""

import csv
import io
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.db.models import PaperSignal, PaperSignalOutcome
from app.services.journal_analytics import ScoreRecord, analyse_score_components

router = APIRouter(prefix="/journal", tags=["Paper journal"])


class JournalSummary(BaseModel):
    total_signals: int
    open_outcomes: int
    resolved_outcomes: int
    wins: int
    losses: int
    win_rate_percent: float | None
    average_realized_r: float | None
    average_mfe_r: float | None
    average_mae_r: float | None


async def _outcomes(session: DbSession, session_date: date | None):
    statement = select(PaperSignal, PaperSignalOutcome).join(
        PaperSignalOutcome, PaperSignalOutcome.paper_signal_id == PaperSignal.id, isouter=True
    )
    if session_date:
        statement = statement.where(PaperSignal.session_date == session_date)
    return (await session.execute(statement.order_by(PaperSignal.created_at.desc()))).all()


@router.get("/summary", response_model=JournalSummary)
async def summary(session: DbSession, _: CurrentUser, session_date: date | None = None) -> JournalSummary:
    rows = await _outcomes(session, session_date)
    outcomes = [outcome for _, outcome in rows if outcome]
    resolved = [item for item in outcomes if item.status in {"TARGET", "STOP"} and item.realized_r is not None]
    wins = sum(item.status == "TARGET" for item in resolved)
    losses = sum(item.status == "STOP" for item in resolved)

    def _average(values: list[float]) -> float | None:
        return float(sum(values) / len(values)) if values else None

    return JournalSummary(
        total_signals=len(rows),
        open_outcomes=sum(item is None or item.status == "OPEN" for item in outcomes),
        resolved_outcomes=len(resolved),
        wins=wins,
        losses=losses,
        win_rate_percent=round(wins * 100 / len(resolved), 2) if resolved else None,
        average_realized_r=_average([item.realized_r for item in resolved]),
        average_mfe_r=_average([item.mfe_r for item in outcomes]),
        average_mae_r=_average([item.mae_r for item in outcomes]),
    )


@router.get("/score-analysis")
async def score_analysis(session: DbSession, _: CurrentUser, strategy_version: str | None = None) -> dict:
    """Outcome lift of each score component across resolved paper signals."""
    rows = await _outcomes(session, None)
    records = [
        ScoreRecord(
            strategy_version=signal.strategy_version,
            score_breakdown=signal.score_breakdown or {},
            status=outcome.status,
            realized_r=float(outcome.realized_r),
        )
        for signal, outcome in rows
        if outcome
        and outcome.status in {"TARGET", "STOP"}
        and outcome.realized_r is not None
        and (strategy_version is None or signal.strategy_version == strategy_version)
    ]
    return analyse_score_components(records)


@router.get("/export.csv")
async def export_csv(
    session: DbSession, _: CurrentUser, session_date: date | None = Query(default=None)
) -> StreamingResponse:
    rows = await _outcomes(session, session_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "signal_id",
            "session_date",
            "instrument",
            "side",
            "entry",
            "stop",
            "target",
            "status",
            "exit",
            "realized_r",
            "mfe_r",
            "mae_r",
            "resolved_at",
        ]
    )
    for signal, outcome in rows:
        writer.writerow(
            [
                signal.id,
                signal.session_date,
                signal.instrument_token,
                signal.side,
                signal.entry_price,
                signal.stop_price,
                signal.target_price,
                outcome.status if outcome else "OPEN",
                outcome.exit_price if outcome else "",
                outcome.realized_r if outcome else "",
                outcome.mfe_r if outcome else "",
                outcome.mae_r if outcome else "",
                outcome.resolved_at if outcome else "",
            ]
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=paper-journal.csv"},
    )
