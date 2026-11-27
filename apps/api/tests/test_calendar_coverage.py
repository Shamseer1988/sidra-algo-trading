"""The NSE calendar says when it is about to run out, and still fails closed when it does.

Until this, the calendar knew 2026 and nothing else, and said nothing about it:
on 1 January 2027 every session would have been refused as "not confirmed",
the open would have reported a calendar refusal, and nothing would have traded
until somebody worked out why.

No 2027 date is asserted anywhere here. NSE had not published its 2027 circular
when this was written, and a guessed holiday is worse than none.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from app.services import scheduler
from app.services.trading_calendar import (
    BUILT_IN_HOLIDAYS,
    CALENDAR_WARNING_DAYS,
    NSE_2026_HOLIDAYS,
    CalendarCoverageState,
    TradingCalendar,
)

# --- the verified year is unchanged ---------------------------------------------


def test_every_built_in_2026_holiday_is_closed_and_named():
    calendar = TradingCalendar()
    for day, name in NSE_2026_HOLIDAYS.items():
        assert calendar.session_for(day) is None
        assert calendar.closed_reason(day) == name


def test_an_ordinary_2026_weekday_trades_and_a_weekend_does_not():
    calendar = TradingCalendar()
    assert calendar.session_for(date(2026, 11, 27)) is not None  # a Friday
    assert calendar.session_for(date(2026, 11, 28)) is None  # Saturday
    assert calendar.closed_reason(date(2026, 11, 29)) == "Weekend"


def test_every_2026_day_answers_exactly_as_before():
    """The old rule, written out: confirmed year, not a weekend, not a listed holiday."""
    calendar = TradingCalendar()
    day = date(2026, 1, 1)
    while day.year == 2026:
        old_answer = day.weekday() < 5 and day not in NSE_2026_HOLIDAYS
        assert (calendar.session_for(day) is not None) == old_answer, day
        day += timedelta(days=1)


def test_no_year_after_2026_is_built_in_until_nse_publishes_it():
    # When the official circular is transcribed, this test is changed with it,
    # and the change names the circular.
    assert set(BUILT_IN_HOLIDAYS) == {2026}


# --- unconfirmed and incomplete years fail closed ---------------------------------


def test_an_unconfirmed_year_fails_closed():
    calendar = TradingCalendar()
    first_weekday = date(2027, 1, 4)
    assert calendar.session_for(first_weekday) is None
    assert "not confirmed" in calendar.closed_reason(first_weekday)
    status = calendar.status_at(datetime(2027, 1, 4, 5, 0, tzinfo=UTC))
    assert not status.trading_day


def test_a_year_confirmed_without_any_holiday_list_still_fails_closed():
    """Adding 2027 to NSE_CALENDAR_CONFIRMED_YEARS alone must not open 26 January."""
    calendar = TradingCalendar(confirmed_years={2026, 2027})
    republic_day = date(2027, 1, 26)
    assert calendar.session_for(republic_day) is None
    assert calendar.session_for(date(2027, 1, 4)) is None
    assert "no holiday list" in calendar.closed_reason(date(2027, 1, 4))


def test_a_confirmed_year_with_operator_holidays_is_usable():
    """The documented route for a year not yet built in: confirm it and supply its closures."""
    closure = date(2027, 1, 26)
    calendar = TradingCalendar(confirmed_years={2026, 2027}, holiday_overrides={closure})
    assert calendar.session_for(closure) is None
    assert calendar.session_for(date(2027, 1, 4)) is not None


# --- coverage, and the year transition ---------------------------------------------


def test_coverage_is_ok_early_in_a_confirmed_year():
    coverage = TradingCalendar().coverage(date(2026, 6, 1))
    assert coverage.state == CalendarCoverageState.OK


def test_coverage_warns_inside_the_window_when_next_year_is_missing():
    calendar = TradingCalendar()
    day = date(2027, 1, 1) - timedelta(days=CALENDAR_WARNING_DAYS)
    coverage = calendar.coverage(day)
    assert coverage.days_until_next_year == CALENDAR_WARNING_DAYS
    assert coverage.state == CalendarCoverageState.WARNING
    assert coverage.next_year == 2027
    assert "2027" in coverage.detail and "1 January" in coverage.detail
    # One day before the window opens it is still quiet.
    assert calendar.coverage(date.fromordinal(day.toordinal() - 1)).state == CalendarCoverageState.OK


def test_coverage_warns_on_the_last_day_of_the_year():
    coverage = TradingCalendar().coverage(date(2026, 12, 31))
    assert coverage.state == CalendarCoverageState.WARNING
    assert coverage.days_until_next_year == 1


def test_coverage_is_critical_once_the_year_has_turned():
    coverage = TradingCalendar().coverage(date(2027, 1, 1))
    assert coverage.state == CalendarCoverageState.CRITICAL
    assert "failing closed" in coverage.detail


def test_coverage_is_critical_for_a_confirmed_year_with_no_holidays():
    coverage = TradingCalendar(confirmed_years={2027}).coverage(date(2027, 3, 1))
    assert coverage.state == CalendarCoverageState.CRITICAL
    assert "no holiday list" in coverage.detail


def test_no_warning_when_next_year_is_ready():
    calendar = TradingCalendar(confirmed_years={2026, 2027}, holiday_overrides={date(2027, 1, 26)})
    coverage = calendar.coverage(date(2026, 12, 20))
    assert coverage.state == CalendarCoverageState.OK
    assert "2027" in coverage.detail


def test_coverage_never_changes_what_the_calendar_decides():
    calendar = TradingCalendar()
    before = [calendar.session_for(date(2026, 12, day)) for day in range(1, 32)]
    calendar.coverage(date(2026, 12, 20))
    assert [calendar.session_for(date(2026, 12, day)) for day in range(1, 32)] == before


# --- the operator is told ---------------------------------------------------------


def test_the_morning_note_carries_the_warning():
    note = scheduler.calendar_note(TradingCalendar(), datetime(2026, 12, 20, 3, 15, tzinfo=UTC))
    assert note is not None and "2027" in note


def test_the_morning_note_is_silent_when_covered():
    assert scheduler.calendar_note(TradingCalendar(), datetime(2026, 6, 1, 3, 15, tzinfo=UTC)) is None


async def test_the_warning_is_appended_to_the_session_open_message(monkeypatch):
    sent: list[str] = []

    class FakeTelegram:
        def __init__(self, _settings):
            pass

        async def send_message(self, text, parse_mode=None):
            sent.append(text)

    async def configured(_settings):
        return SimpleNamespace(telegram_is_configured=True)

    async def muted(_session, _key):
        return False  # session-open alerts muted; the calendar warning must still go out

    monkeypatch.setattr("app.services.telegram.TelegramNotificationService", FakeTelegram)
    monkeypatch.setattr("app.services.telegram_config.configured_settings", configured)
    monkeypatch.setattr("app.services.notification_settings.wants", muted)

    result = SimpleNamespace(opened=True, detail="Armed", expires_at=None, step="armed", findings=[])
    await scheduler.send_session_open_alert(SimpleNamespace(), result, calendar_warning="NSE calendar for 2027 ...")
    assert len(sent) == 1 and "Calendar:" in sent[0] and "2027" in sent[0]

    sent.clear()
    await scheduler.send_session_open_alert(SimpleNamespace(), result)
    assert sent == []  # muted and nothing to warn about: unchanged behaviour


def test_the_system_overview_reports_the_calendar():
    from app.api.routes.system import calendar_status

    status = calendar_status(datetime(2026, 12, 20, 6, 0, tzinfo=UTC))
    assert status.status == "warning" and "2027" in status.detail
    assert calendar_status(datetime(2026, 6, 1, 6, 0, tzinfo=UTC)).status == "healthy"
