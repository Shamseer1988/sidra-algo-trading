"""Which Telegram messages may be switched off, and which may never be.

Two messages arrived for every signal -- the paper journal's "TRADING SIGNAL
MATCHED" and the live path's approval request -- describing the same trade. The
fix is a preference, and the risk in a preference like this is obvious: the
easiest way to make Telegram quieter is to mute the messages that say something
broke, and then the operator believes they are being told when they are not.

So the tests that matter most here are the ones asserting what CANNOT be muted.
"""

import pytest

from app.services import notification_settings as module
from app.services.notification_settings import DEFAULTS, NotificationSettings


class FakeSession:
    def __init__(self, value=None, raises: Exception | None = None) -> None:
        self._value = value
        self._raises = raises

    async def get(self, _model, _key):
        if self._raises:
            raise self._raises
        if self._value is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(value=self._value)


# --- defaults change nothing ----------------------------------------------


async def test_a_deployment_that_never_opened_the_screen_sends_everything() -> None:
    """The feature is inert until somebody chooses otherwise."""
    loaded = await module.load(FakeSession())
    assert loaded == DEFAULTS
    assert all(value is True for value in loaded.model_dump().values())


async def test_an_unreadable_row_falls_back_rather_than_raising() -> None:
    """Losing a preference is a nuisance; raising here would cost the alert."""
    assert await module.load(FakeSession({"paper_signal_alerts": "not a bool"})) == DEFAULTS
    assert await module.load(FakeSession("not a dict")) == DEFAULTS


async def test_a_partial_row_keeps_the_defaults_for_what_it_omits() -> None:
    """A field added later must not read as False on an older row."""
    loaded = await module.load(FakeSession({"paper_signal_alerts": False}))
    assert loaded.paper_signal_alerts is False
    assert loaded.session_open_alerts is True
    assert loaded.order_sent_confirmations is True


# --- the preference is honoured -------------------------------------------


@pytest.mark.parametrize("field", ["paper_signal_alerts", "session_open_alerts", "order_sent_confirmations"])
async def test_each_switch_is_readable_through_wants(field: str) -> None:
    assert await module.wants(FakeSession({field: False}), field) is False
    assert await module.wants(FakeSession({field: True}), field) is True


async def test_a_failed_lookup_sends_the_message() -> None:
    """Fails toward telling the operator.

    A preference lookup that errored and silenced an alert would be the worst of
    both: the operator believes they are being told, and they are not.
    """
    assert await module.wants(FakeSession(raises=RuntimeError("database gone")), "paper_signal_alerts") is True
    assert await module.wants(FakeSession(), "a_field_that_does_not_exist") is True


# --- what may never be switched off ---------------------------------------


def test_no_switch_exists_for_any_failure_message() -> None:
    """The guard that gives this feature its shape.

    Every field is a thing going right. If a future field names a rejection, a
    block, a failure or an unprotected position, this fails -- because the way
    to make a safety alert optional is to add it here, and that must not be a
    quiet change.
    """
    forbidden = ("reject", "fail", "block", "unprotected", "refus", "error", "halt", "stop")
    for field in NotificationSettings.model_fields:
        assert not any(word in field for word in forbidden), (
            f"{field!r} reads like a failure notice. Muting a failure does not reduce how often things go "
            "wrong, only how often you hear about it."
        )


def test_the_always_sent_list_names_the_safety_messages() -> None:
    """The screen states these, so they must actually be stated."""
    joined = " ".join(module.ALWAYS_SENT).lower()
    for expected in ("approval", "reject", "stop", "reconciliation", "exit", "refus"):
        assert expected in joined, f"the always-sent list does not mention {expected!r}"


def test_every_switch_is_described_for_the_screen() -> None:
    """A toggle with no explanation is a toggle nobody can decide about."""
    assert set(module.DESCRIPTIONS) == set(NotificationSettings.model_fields)
    for label, help_text in module.DESCRIPTIONS.values():
        assert label and len(help_text) > 60


def test_the_senders_check_the_fields_that_exist() -> None:
    """A preference nothing reads is a switch that silently does nothing.

    Asserted against the source because the call sites are in three modules and
    nothing else ties a field name to the code that honours it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "services"
    sources = {
        "paper_signal_alerts": (root / "scanner_orchestration.py").read_text(),
        "session_open_alerts": (root / "scheduler.py").read_text(),
        "order_sent_confirmations": (root / "live_entry_bridge.py").read_text(),
    }
    assert set(sources) == set(NotificationSettings.model_fields), "a field has no sender wired to it"
    for field, source in sources.items():
        assert f'"{field}"' in source, f"{field} is never checked by its sender"
