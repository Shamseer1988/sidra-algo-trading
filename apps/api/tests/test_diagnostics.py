"""U5: the read-only report scripts, run from Reports > Diagnostics.

The claims that matter: only allow-listed scripts run, with arguments built
from typed values (never request text); a script runs as its own process and
its output comes back; a configured secret never leaves the server; and one
report runs at a time.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import diagnostics as routes
from app.core.config import get_settings
from app.services import report_runner

USER = SimpleNamespace(id=None)


def test_only_the_read_only_scripts_are_listed():
    assert set(report_runner.REPORTS) == {
        "daily_review",
        "why_no_trade",
        "inspect_live_day",
        "check_settings",
        "audit_deployment",
        "strategy_stats",
        "watchlist",
    }
    for name in report_runner.REPORTS:
        assert (report_runner.SCRIPTS_DIR / f"{name}.py").is_file()


@pytest.mark.parametrize("name", ["apply_settings", "../scripts/apply_settings", "purge_history", "run_sweep", ""])
async def test_anything_else_is_refused(name):
    with pytest.raises(HTTPException) as refused:
        await routes.run_report(name, routes.RunRequest(), get_settings(), USER)
    assert refused.value.status_code == 409


def test_arguments_come_only_from_typed_values_and_the_reports_own_options():
    review = report_runner.REPORTS["daily_review"]
    assert report_runner.arguments(
        review, day=date(2026, 11, 20), start=date(2026, 11, 1), end=None, after="10:00", no_broker=True
    ) == ["--date", "2026-11-20"]
    why = report_runner.REPORTS["why_no_trade"]
    assert report_runner.arguments(why, day=None, start=None, end=None, after="11:15", no_broker=False) == [
        "--after",
        "11:15",
    ]
    with pytest.raises(report_runner.ReportError):
        report_runner.arguments(why, day=None, start=None, end=None, after="11:15; rm -rf /", no_broker=False)


def test_a_configured_secret_is_replaced_in_the_output():
    settings = get_settings().model_copy(update={"telegram_bot_token": "999999:VERY-SECRET-TOKEN"})
    text = report_runner.redact("token=999999:VERY-SECRET-TOKEN and nothing else", settings)
    assert "VERY-SECRET" not in text and "•••" in text


async def test_settings_check_runs_as_its_own_process_and_shows_the_desks():
    result = await routes.run_report("check_settings", routes.RunRequest(), get_settings(), USER)
    assert result.command == "python scripts/check_settings.py"
    assert "0. Trading desks and strategy modes" in result.output
    assert "intraday desk" in result.output


async def test_one_report_at_a_time():
    async with report_runner._lock:
        with pytest.raises(HTTPException) as busy:
            await routes.run_report("watchlist", routes.RunRequest(), get_settings(), USER)
    assert "Another report" in busy.value.detail


async def test_the_list_says_which_options_each_report_takes():
    listed = {item.name: item for item in await routes.reports(USER)}
    assert listed["daily_review"].options == ["date"]
    assert listed["check_settings"].options == []
