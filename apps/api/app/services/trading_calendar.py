"""NSE cash-market calendar and session-state classification.

The built-in 2026 closures are sourced from NSE/CMTR/71775 and the 15 January
amendment. Future years fail closed until an operator explicitly confirms that
year and supplies any additional holiday or special-session overrides.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.core.config import Settings

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
PRE_OPEN = time(9, 0)
REGULAR_OPEN = time(9, 15)
REGULAR_CLOSE = time(15, 30)
POST_MARKET_CLOSE = time(16, 0)

NSE_2026_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 15): "Municipal Corporation Election in Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Babasaheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}


class MarketPhase(StrEnum):
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    POST_MARKET = "POST_MARKET"


@dataclass(frozen=True)
class TradingSession:
    trading_date: date
    regular_open: time = REGULAR_OPEN
    regular_close: time = REGULAR_CLOSE
    pre_open: time = PRE_OPEN
    post_market_close: time = POST_MARKET_CLOSE
    is_special: bool = False
    label: str = "NSE regular session"


@dataclass(frozen=True)
class MarketStatus:
    phase: MarketPhase
    trading_day: bool
    reason: str
    local_timestamp: datetime
    session: TradingSession | None

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "trading_day": self.trading_day,
            "reason": self.reason,
            "local_timestamp": self.local_timestamp.isoformat(),
            "session_date": self.session.trading_date.isoformat() if self.session else None,
            "regular_open": self.session.regular_open.isoformat(timespec="minutes") if self.session else None,
            "regular_close": self.session.regular_close.isoformat(timespec="minutes") if self.session else None,
            "is_special_session": self.session.is_special if self.session else False,
        }


def _parse_dates(value: str) -> set[date]:
    dates: set[date] = set()
    for item in value.split(","):
        if item.strip():
            dates.add(date.fromisoformat(item.strip()))
    return dates


def _parse_special_sessions(value: str) -> dict[date, TradingSession]:
    sessions: dict[date, TradingSession] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        date_part, hours = item.split("@", 1)
        open_part, close_part = hours.split("-", 1)
        session_date = date.fromisoformat(date_part)
        sessions[session_date] = TradingSession(
            trading_date=session_date,
            pre_open=time.fromisoformat(open_part),
            regular_open=time.fromisoformat(open_part),
            regular_close=time.fromisoformat(close_part),
            post_market_close=time.fromisoformat(close_part),
            is_special=True,
            label="NSE special trading session",
        )
    return sessions


class TradingCalendar:
    """Classifies NSE session state using confirmed calendars and overrides."""

    def __init__(
        self,
        *,
        confirmed_years: set[int] | None = None,
        holiday_overrides: set[date] | None = None,
        special_sessions: dict[date, TradingSession] | None = None,
    ) -> None:
        self._confirmed_years = confirmed_years or {2026}
        self._holidays = dict(NSE_2026_HOLIDAYS)
        self._holidays.update({item: "Operator-configured exchange holiday" for item in holiday_overrides or set()})
        self._special_sessions = special_sessions or {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "TradingCalendar":
        try:
            holiday_overrides = _parse_dates(settings.nse_holiday_overrides)
            special_sessions = _parse_special_sessions(settings.nse_special_sessions)
        except ValueError as exc:
            raise ValueError("Invalid NSE calendar override configuration") from exc
        return cls(
            confirmed_years=settings.calendar_confirmed_years,
            holiday_overrides=holiday_overrides,
            special_sessions=special_sessions,
        )

    def session_for(self, trading_date: date) -> TradingSession | None:
        special = self._special_sessions.get(trading_date)
        if special is not None:
            return special
        if trading_date.year not in self._confirmed_years:
            return None
        if trading_date.weekday() >= 5 or trading_date in self._holidays:
            return None
        return TradingSession(trading_date=trading_date)

    def closed_reason(self, trading_date: date) -> str:
        if trading_date in self._special_sessions:
            return "Special trading session"
        if trading_date.year not in self._confirmed_years:
            return f"NSE calendar for {trading_date.year} is not confirmed; failing closed"
        if trading_date in self._holidays:
            return self._holidays[trading_date]
        if trading_date.weekday() >= 5:
            return "Weekend"
        return "Outside exchange session"

    def status_at(self, timestamp: datetime) -> MarketStatus:
        local = timestamp.astimezone(MARKET_TIMEZONE)
        session = self.session_for(local.date())
        if session is None:
            return MarketStatus(MarketPhase.CLOSED, False, self.closed_reason(local.date()), local, None)
        local_time = local.time().replace(tzinfo=None)
        if session.pre_open <= local_time < session.regular_open:
            phase = MarketPhase.PRE_OPEN
            reason = "NSE pre-open session"
        elif session.regular_open <= local_time < session.regular_close:
            phase = MarketPhase.OPEN
            reason = session.label
        elif session.regular_close <= local_time < session.post_market_close:
            phase = MarketPhase.POST_MARKET
            reason = "NSE post-market session"
        else:
            phase = MarketPhase.CLOSED
            reason = "Outside exchange session"
        return MarketStatus(phase, True, reason, local, session)

    def is_open(self, timestamp: datetime) -> bool:
        return self.status_at(timestamp).phase == MarketPhase.OPEN


DEFAULT_TRADING_CALENDAR = TradingCalendar()
