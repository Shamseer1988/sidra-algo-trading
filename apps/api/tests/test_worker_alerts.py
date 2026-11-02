"""Saying out loud that the scanner has stopped.

The worker wrote its state to Redis and logged every failed iteration. Nobody
reads either. On 29 October it logged the same connection error every thirty
seconds for four days, stored no candles, and placed no orders on three
trading days -- found because the operator noticed the absence of trades.

Two things decide whether this alert is worth having, and both are the
awkward ones: it must reach Telegram when the database is down, because the
database is usually what is broken; and it must never raise, because it fires
from inside the handler for the first failure.
"""

from types import SimpleNamespace

import pytest

from app.services import worker_alerts


class Sent:
    """Collects what would have gone to Telegram."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.messages: list[str] = []
        self.settings: list[object] = []
        self.fail = fail

    def service(self, settings: object) -> "Sent":
        self.settings.append(settings)
        return self

    async def send_message(self, text: str, parse_mode: str | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.messages.append(text)


def settings(configured: bool = True, token: str = "env-token") -> SimpleNamespace:
    return SimpleNamespace(telegram_is_configured=configured, telegram_bot_token=token, telegram_chat_id="1")


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Sent:
    sent = Sent()
    monkeypatch.setattr(
        "app.services.telegram.TelegramNotificationService", lambda cfg: sent.service(cfg), raising=False
    )
    return sent


@pytest.fixture
def stored_credentials(monkeypatch: pytest.MonkeyPatch):
    """Patch the database-backed lookup. Each test says how it behaves."""

    def use(result):
        async def configured_settings(base):
            if isinstance(result, Exception):
                raise result
            return result if result is not None else base

        monkeypatch.setattr("app.services.telegram_config.configured_settings", configured_settings, raising=False)

    return use


# --- it reaches Telegram when the database does not ----------------------


async def test_it_still_alerts_when_the_stored_credentials_cannot_be_read(telegram: Sent, stored_credentials, caplog):
    """The database is the usual casualty, and the alert is about that.

    Reading the token from it and giving up on failure would mean the one
    outage worth announcing is the one that cannot be announced.
    """
    stored_credentials(OSError("connection refused"))
    await worker_alerts.alert_worker_degraded(settings(), error="Connect call failed", failures=5)
    assert len(telegram.messages) == 1
    assert "SCANNER STOPPED WORKING" in telegram.messages[0]


async def test_stored_credentials_are_preferred_when_they_can_be_read(telegram: Sent, stored_credentials):
    stored = settings(token="stored-token")
    stored_credentials(stored)
    await worker_alerts.alert_worker_degraded(settings(), error="boom", failures=5)
    assert telegram.settings == [stored]


async def test_nothing_is_sent_when_telegram_was_never_configured(telegram: Sent, stored_credentials):
    stored_credentials(None)
    await worker_alerts.alert_worker_degraded(settings(configured=False), error="boom", failures=5)
    assert telegram.messages == []


# --- it never becomes the second failure ---------------------------------


@pytest.mark.parametrize(
    "alert",
    [
        worker_alerts.alert_worker_degraded,
        worker_alerts.alert_worker_giving_up,
    ],
)
async def test_a_telegram_outage_does_not_raise(alert, monkeypatch: pytest.MonkeyPatch, stored_credentials):
    """This runs inside the handler for the failure it is reporting."""
    stored_credentials(None)
    broken = Sent(fail=RuntimeError("Telegram API request failed"))
    monkeypatch.setattr(
        "app.services.telegram.TelegramNotificationService", lambda cfg: broken.service(cfg), raising=False
    )
    await alert(settings(), error="boom", failures=5)


async def test_a_recovery_message_also_cannot_raise(monkeypatch: pytest.MonkeyPatch, stored_credentials):
    stored_credentials(None)
    broken = Sent(fail=RuntimeError("Telegram API request failed"))
    monkeypatch.setattr(
        "app.services.telegram.TelegramNotificationService", lambda cfg: broken.service(cfg), raising=False
    )
    await worker_alerts.alert_worker_recovered(settings(), after_failures=6)


# --- what each message says ----------------------------------------------


async def test_the_degraded_message_names_the_error_and_the_consequence(telegram: Sent, stored_credentials):
    stored_credentials(None)
    await worker_alerts.alert_worker_degraded(
        settings(), error="[Errno 111] Connect call failed ('127.0.0.1', 5433)", failures=5
    )
    text = telegram.messages[0]
    assert "127.0.0.1" in text
    assert "no order can be placed" in text


async def test_the_giving_up_message_says_what_silence_would_mean(telegram: Sent, stored_credentials):
    stored_credentials(None)
    await worker_alerts.alert_worker_giving_up(settings(), error="boom", failures=20)
    text = telegram.messages[0]
    assert "20 consecutive failures" in text
    assert "no recovery message follows" in text


async def test_the_recovery_message_closes_the_alarm(telegram: Sent, stored_credentials):
    stored_credentials(None)
    await worker_alerts.alert_worker_recovered(settings(), after_failures=7)
    assert "Scanner recovered" in telegram.messages[0]
    assert "7 failed attempt" in telegram.messages[0]


# --- when it fires --------------------------------------------------------


def test_the_operator_hears_within_about_a_minute():
    """Five failures against the loop's 2, 4, 8, 16, 30 backoff. Past a blip,
    and well before the ten minutes it takes to give up."""
    from app.services.worker_supervision import RestartBackoff
    from app.worker import GIVE_UP_AFTER_FAILURES

    backoff = RestartBackoff(maximum_seconds=30)
    waited = sum(backoff.record_failure(now=0) for _ in range(worker_alerts.ALERT_AFTER_FAILURES))

    assert waited <= 90
    assert worker_alerts.ALERT_AFTER_FAILURES < GIVE_UP_AFTER_FAILURES
