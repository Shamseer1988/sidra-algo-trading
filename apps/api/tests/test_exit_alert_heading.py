"""The exit alert's heading says what happened, not that something did.

16 November: thirteen "no working stop" alerts went out under a green tick and
"POSITION CLOSED" while every stop behind them was being rejected and nothing
had been closed.
"""

from types import SimpleNamespace

import pytest

import app.services.telegram as telegram_module
import app.services.telegram_config as telegram_config
from app.services import scheduler
from app.services.live_exit_manager import PositionExit


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []

    class FakeTelegram:
        def __init__(self, _settings) -> None:  # noqa: ANN001
            pass

        async def send_message(self, text: str, parse_mode: str | None = None) -> None:
            messages.append(text)

    async def configured(_settings):  # noqa: ANN001
        return SimpleNamespace(telegram_is_configured=True)

    monkeypatch.setattr(telegram_module, "TelegramNotificationService", FakeTelegram)
    monkeypatch.setattr(telegram_config, "configured_settings", configured)
    return messages


def result(*items: PositionExit) -> SimpleNamespace:
    return SimpleNamespace(noteworthy=list(items))


@pytest.mark.asyncio
async def test_a_stop_sent_again_is_a_warning_not_a_closed_position(sent) -> None:
    item = PositionExit("NESTLEIND", True, "unprotected", "22 of NESTLEIND was open with no working stop behind it.")
    await scheduler._send_exit_alert(SimpleNamespace(), result(item))
    assert "NO WORKING STOP" in sent[0]
    assert "POSITION CLOSED" not in sent[0]


@pytest.mark.asyncio
async def test_rejected_stops_closed_at_market_say_so(sent) -> None:
    item = PositionExit("ETERNAL", True, "stops_rejected_closing", "closed at market instead of stopped again.")
    await scheduler._send_exit_alert(SimpleNamespace(), result(item))
    assert "STOPS REJECTED" in sent[0]


@pytest.mark.asyncio
async def test_a_real_exit_is_still_position_closed(sent) -> None:
    item = PositionExit("RVNL", True, "exited", "Target reached. Exit sent for 50.")
    await scheduler._send_exit_alert(SimpleNamespace(), result(item))
    assert "POSITION CLOSED" in sent[0]


@pytest.mark.asyncio
async def test_a_failure_still_needs_attention(sent) -> None:
    item = PositionExit("RVNL", False, "cancel_failed", "Close this by hand.")
    await scheduler._send_exit_alert(SimpleNamespace(), result(item))
    assert "EXIT NEEDS ATTENTION" in sent[0]
