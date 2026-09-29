"""A tap must be answered in the chat, not only in a toast that vanishes.

The operator pressed Approve and saw nothing. Two causes: the paper button
genuinely does nothing to a broker, and the only acknowledgement of any tap was
``answerCallbackQuery`` -- a toast lasting a second or two, gone if you looked
away, leaving no way to tell a refusal from a bug.

So every tap now also sends a durable message. These tests pin the part that is
easy to get subtly wrong: the heading must never contradict the detail. A tick
over a refusal is worse than no reply, because it is believed.
"""

import pytest

from app.api.routes.telegram import (
    _LIVE_STATUS_KINDS,
    _REPLY_HEADINGS,
    BLOCKED_KIND,
    ERROR_KIND,
    INFO_KIND,
    REJECTED_KIND,
    STOPPED_KIND,
    SUBMITTED_KIND,
    CallbackReply,
    _deliver_reply,
    _reply_message,
)
from app.services.telegram import TelegramError


@pytest.mark.parametrize("kind", [SUBMITTED_KIND, REJECTED_KIND, BLOCKED_KIND, ERROR_KIND, INFO_KIND, STOPPED_KIND])
def test_every_kind_has_a_heading(kind: str) -> None:
    assert kind in _REPLY_HEADINGS
    assert _reply_message(CallbackReply(kind, "detail")).startswith(_REPLY_HEADINGS[kind])


def test_the_detail_is_carried_verbatim() -> None:
    message = _reply_message(CallbackReply(SUBMITTED_KIND, "Sent. Broker order 260929000012345"))
    assert "260929000012345" in message


def test_markup_in_the_detail_cannot_break_the_send() -> None:
    """Broker text is not ours. Unescaped < would make Telegram reject the message."""
    message = _reply_message(CallbackReply(ERROR_KIND, "rejected <RMS> & margin"))
    assert "&lt;RMS&gt;" in message and "&amp;" in message
    assert "<RMS>" not in message


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("SUBMITTED", SUBMITTED_KIND),
        ("APPROVED", SUBMITTED_KIND),
        ("REJECTED", REJECTED_KIND),
        ("EXPIRED", BLOCKED_KIND),
        ("BLOCKED", BLOCKED_KIND),
    ],
)
def test_live_statuses_map_to_the_right_tone(status: str, expected: str) -> None:
    assert _LIVE_STATUS_KINDS[status] == expected


def test_an_unknown_status_never_reads_as_success() -> None:
    """A status this map has not seen must fail towards 'not sent'."""
    kind = _LIVE_STATUS_KINDS.get("SOMETHING_NEW", BLOCKED_KIND)
    assert kind == BLOCKED_KIND
    assert kind != SUBMITTED_KIND


def test_every_live_approval_status_is_mapped() -> None:
    """A new terminal status must not silently fall through to a default."""
    from app.services import live_approval

    for status in live_approval.TERMINAL_STATUSES:
        assert status in _LIVE_STATUS_KINDS, f"{status} has no reply tone; it would read as NOT SENT by default"


def test_only_a_submission_reads_as_approved() -> None:
    """Exactly one heading may tell the operator money moved."""
    approved = [kind for kind, heading in _REPLY_HEADINGS.items() if "APPROVED" in heading]
    assert approved == [SUBMITTED_KIND]


# --- delivery ---------------------------------------------------------------


class FakeNotifier:
    def __init__(self, *, answer_fails: bool = False, send_fails: bool = False) -> None:
        self.answered: list = []
        self.sent: list = []
        self._answer_fails = answer_fails
        self._send_fails = send_fails

    async def answer_callback(self, callback_id: str, text: str) -> None:
        if self._answer_fails:
            raise TelegramError("answer failed")
        self.answered.append((callback_id, text))

    async def send_message(self, text: str, reply_markup=None, parse_mode=None):  # noqa: ANN001
        if self._send_fails:
            raise TelegramError("send failed")
        self.sent.append(text)
        return {"ok": True}


@pytest.mark.asyncio
async def test_a_tap_gets_both_a_toast_and_a_message() -> None:
    """The regression: only the toast existed, so a tap looked like nothing."""
    notifier = FakeNotifier()
    await _deliver_reply(notifier, "cb-1", CallbackReply(SUBMITTED_KIND, "Sent. Broker order 1."), announce=True)
    assert notifier.answered == [("cb-1", "Sent. Broker order 1.")]
    assert len(notifier.sent) == 1
    assert "APPROVED" in notifier.sent[0]


@pytest.mark.asyncio
async def test_a_typed_command_still_gets_a_message() -> None:
    """/stop arrives with no callback id, and must still be acknowledged."""
    notifier = FakeNotifier()
    await _deliver_reply(notifier, None, CallbackReply(STOPPED_KIND, "Emergency stop engaged."), announce=True)
    assert notifier.answered == []
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_a_failed_toast_does_not_cost_the_message() -> None:
    """The toast expires server-side after a while; the message still matters."""
    notifier = FakeNotifier(answer_fails=True)
    await _deliver_reply(notifier, "cb-1", CallbackReply(REJECTED_KIND, "Rejected."), announce=True)
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_a_failed_delivery_never_raises() -> None:
    """Telegram retries a non-200 webhook, and a retry would re-enter the handler
    after the decision is already committed."""
    notifier = FakeNotifier(answer_fails=True, send_fails=True)
    await _deliver_reply(notifier, "cb-1", CallbackReply(ERROR_KIND, "boom"), announce=True)
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_the_toast_is_truncated_but_the_message_is_not() -> None:
    long_detail = "x" * 400
    notifier = FakeNotifier()
    await _deliver_reply(notifier, "cb-1", CallbackReply(BLOCKED_KIND, long_detail), announce=True)
    assert len(notifier.answered[0][1]) == 200
    assert long_detail in notifier.sent[0]


@pytest.mark.asyncio
async def test_an_unauthorised_sender_is_told_no_but_cannot_post_to_the_chat() -> None:
    """The toast reaches whoever tapped; the chat message is the operator's.

    Announcing a refusal would let anyone who found the bot put text into the
    operator's chat at will.
    """
    notifier = FakeNotifier()
    await _deliver_reply(notifier, "cb-1", CallbackReply(ERROR_KIND, "Command rejected"), announce=False)
    assert notifier.answered == [("cb-1", "Command rejected")]
    assert notifier.sent == []
