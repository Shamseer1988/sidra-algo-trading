"""Small deterministic primitives used by the scanner worker supervisor."""

import asyncio
import time
from dataclasses import dataclass

WORKER_STATE_KEY = "scanner:worker_state"


@dataclass
class RestartBackoff:
    initial_seconds: float = 2
    maximum_seconds: float = 60
    failures: int = 0
    next_start_at: float = 0

    def ready(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.next_start_at

    def record_failure(self, now: float | None = None) -> float:
        current = now if now is not None else time.monotonic()
        delay = min(self.initial_seconds * (2**self.failures), self.maximum_seconds)
        self.failures += 1
        self.next_start_at = current + delay
        return delay

    def reset(self) -> None:
        self.failures = 0
        self.next_start_at = 0


def completed_task_detail(task: asyncio.Task[None]) -> str:
    if task.cancelled():
        return "Market-data task was cancelled"
    error = task.exception()
    if error is None:
        return "Market-data task ended unexpectedly"
    return f"{type(error).__name__}: {error}"
