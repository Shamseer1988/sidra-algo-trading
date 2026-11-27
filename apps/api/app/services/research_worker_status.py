"""Reading the research worker's heartbeat, apart from the worker so the API need not import it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

WORKER_HEARTBEAT_KEY = "research:worker:heartbeat"
# The worker writes every 5-30 seconds; three missed minutes means it is gone.
WORKER_SILENT_AFTER = timedelta(minutes=3)


@dataclass(frozen=True)
class WorkerSeen:
    status: str
    detail: str
    last_seen: datetime | None


def parse_heartbeat(raw: str | None, now: datetime) -> WorkerSeen:
    """``"<iso time>|open|<worker id>"`` -> healthy / stale / offline, with a sentence."""
    if not raw:
        return WorkerSeen(
            "offline", "No research worker has reported. Backtests and sweeps will wait in the queue.", None
        )
    try:
        stamp, window, worker = raw.split("|", 2)
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return WorkerSeen("unknown", "The research worker's heartbeat could not be read.", None)
    if now - seen > WORKER_SILENT_AFTER:
        return WorkerSeen("stale", f"Last heard from {worker} at {seen.isoformat()}.", seen)
    waiting = " It is waiting for the research window." if window == "closed" else ""
    return WorkerSeen("healthy", f"{worker} is running.{waiting}", seen)
