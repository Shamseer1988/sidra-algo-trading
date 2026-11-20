"""Reports > Diagnostics: the read-only report scripts, run from the screen.

Only the scripts CLAUDE.md lists as read-only and safe at any time, each with
the options it accepts, and nothing else: the name and every argument come
from this allow-list, never from the request as text. Each runs as its own
process (``python scripts/<name>.py``), exactly as an operator would run it
over SSH, so its printing cannot mix with the API's own output, and it is
stopped after ``TIMEOUT_SECONDS``. One at a time.

The scripts already report credentials as "configured: yes". As a second
guard, any configured secret value that still appears in the output is
replaced before it leaves the server.
"""

import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.core.config import Settings

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
TIMEOUT_SECONDS = 180
MAX_OUTPUT = 400_000
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class Report:
    name: str
    title: str
    help: str
    options: tuple[str, ...] = ()  # "date", "from", "to", "after", "no_broker"
    when: str = "Any time"


REPORTS: dict[str, Report] = {
    report.name: report
    for report in (
        Report(
            "daily_review",
            "Daily review",
            "The day in one place: trades, P&L after charges, what was refused and why, and any change made that day.",
            ("date",),
            "After 3:30 PM for the full day",
        ),
        Report(
            "why_no_trade",
            "Why no trade",
            "Every evaluation of the day and the first rule that stopped it, so a quiet day can be explained.",
            ("date", "after"),
        ),
        Report(
            "inspect_live_day",
            "Live day inspection",
            "Live orders, fills and protective stops for one day, checked against what the broker says.",
            ("date", "no_broker"),
        ),
        Report(
            "check_settings",
            "Settings check",
            "Every trading setting in force now, read back from the database, with what each one allows.",
        ),
        Report(
            "audit_deployment",
            "Deployment audit",
            "Whether this deployment is configured to trade: credentials (yes/no only), modes, strategies and limits.",
        ),
        Report(
            "strategy_stats",
            "Strategy statistics",
            "Each strategy's trades, win rate and rupees a trade over a range of sessions.",
            ("from", "to"),
        ),
        Report("watchlist", "Watchlist", "The shares the scanner watches, with their sectors."),
    )
}

_lock = asyncio.Lock()


class ReportError(RuntimeError):
    pass


@dataclass
class ReportResult:
    name: str
    command: str
    exit_code: int
    output: str
    started_at: datetime
    seconds: float
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


def arguments(
    report: Report, *, day: date | None, start: date | None, end: date | None, after: str | None, no_broker: bool
) -> list[str]:
    """The command-line arguments, built only from typed values and this report's options."""
    args: list[str] = []
    if day is not None and "date" in report.options:
        args += ["--date", day.isoformat()]
    if start is not None and "from" in report.options:
        args += ["--from", start.isoformat()]
    if end is not None and "to" in report.options:
        args += ["--to", end.isoformat()]
    if after and "after" in report.options:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", after):
            raise ReportError("'After' must be a time like 11:15.")
        args += ["--after", after]
    if no_broker and "no_broker" in report.options:
        args.append("--no-broker")
    return args


SECRET_FIELDS = (
    "jwt_secret",
    "upstox_access_token",
    "upstox_api_secret",
    "upstox_token_encryption_key",
    "upstox_pin",
    "upstox_totp_secret",
    "firstock_api_key",
    "firstock_password",
    "firstock_totp_secret",
    "telegram_bot_token",
    "telegram_webhook_secret",
    "telegram_config_encryption_key",
)


def redact(text: str, settings: Settings) -> str:
    """Replace any configured secret (and the database password) that appears in the output."""
    values = [str(getattr(settings, name, "") or "") for name in SECRET_FIELDS]
    password = getattr(settings.database_url, "password", None) if hasattr(settings, "database_url") else None
    if password:
        values.append(str(password))
    for value in sorted({v for v in values if len(v) >= 6}, key=len, reverse=True):
        text = text.replace(value, "•••")
    return text


async def run(
    settings: Settings,
    name: str,
    *,
    day: date | None = None,
    start: date | None = None,
    end: date | None = None,
    after: str | None = None,
    no_broker: bool = False,
) -> ReportResult:
    report = REPORTS.get(name)
    if report is None:
        raise ReportError("No such report.")
    script = SCRIPTS_DIR / f"{report.name}.py"
    if not script.is_file():
        raise ReportError(f"The {report.title} script is not in this deployment.")
    args = arguments(report, day=day, start=start, end=end, after=after, no_broker=no_broker)
    if _lock.locked():
        raise ReportError("Another report is running. Try again in a moment.")
    async with _lock:
        started = datetime.now()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            *args,
            cwd=str(SCRIPTS_DIR.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=None,
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ReportError(f"The report did not finish within {TIMEOUT_SECONDS} seconds and was stopped.") from None
        seconds = (datetime.now() - started).total_seconds()
    text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
    truncated = len(text) > MAX_OUTPUT
    if truncated:
        text = text[:MAX_OUTPUT]
    return ReportResult(
        name=report.name,
        command=" ".join(["python", f"scripts/{report.name}.py", *args]),
        exit_code=process.returncode or 0,
        output=redact(text, settings),
        started_at=started,
        seconds=round(seconds, 1),
        truncated=truncated,
    )
