"""Which Telegram messages this system sends, chosen in the UI.

Every signal produced two messages: "TRADING SIGNAL MATCHED" from the paper
journal and "LIVE ORDER — approval required" from the live path, describing the
same trade. Two notifications for one decision is not twice the information; it
is a habit of skimming, and the one with the buttons on it is the one that must
be read.

**Not every message is offered as a toggle, and the split is the whole design.**
A notification that reports what happened can be turned off. A notification that
*is* a safety mechanism cannot, because switching it off does not reduce noise,
it removes the only way the operator learns something went wrong. The brief this
system was built to says a screen may be hidden but a required safety service
may not be removed, and the same rule applies here.

    offered     the paper signal alert, the session-open notice, and the
                confirmation that an automatic order was accepted

    always on   the approval request (you cannot approve what you were not
                sent), a rejected or failed order, an unprotected position, a
                blocked reconciliation, an exit that needs attention, and any
                refusal on the live path

So the switches below are all of the form "stop telling me about things that
went right". Nothing here can silence a failure.

**Defaults keep today's behaviour.** A deployment that never opens this screen
sends exactly what it sent before, so the feature is inert until somebody
chooses otherwise.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting

NOTIFICATION_KEY = "telegram_notifications"


class NotificationSettings(BaseModel):
    """The messages an operator may switch off.

    Each field is phrased so that ``True`` means "send it", because a screen of
    switches where some are inverted is a screen that gets read wrongly once and
    then trusted.
    """

    paper_signal_alerts: bool = Field(
        default=True,
        description="TRADING SIGNAL MATCHED — the paper journal's record of a signal.",
    )
    session_open_alerts: bool = Field(
        default=True,
        description="Live session open — the morning confirmation that arming and reconciliation succeeded.",
    )
    order_sent_confirmations: bool = Field(
        default=True,
        description="LIVE ORDER SENT — an automatic order the broker accepted. Rejections always send.",
    )


DEFAULTS = NotificationSettings()

# What each switch controls, in the operator's words rather than the field's.
# Kept beside the model so the screen and the server cannot describe the same
# switch differently.
DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "paper_signal_alerts": (
        "Paper signal alerts",
        "The “TRADING SIGNAL MATCHED” message. Under TELEGRAM_APPROVAL every live signal also sends an "
        "approval request describing the same trade, so leaving this on means two messages per decision.",
    ),
    "session_open_alerts": (
        "Session open notice",
        "The morning “Live session open” confirmation that the broker logged in, reconciliation passed and "
        "the system armed. Turning it off does not stop the session opening — only the message saying it did.",
    ),
    "order_sent_confirmations": (
        "Automatic order confirmations",
        "The “LIVE ORDER SENT” message for an order placed without asking. A rejected order, an unprotected "
        "position and every refusal still send, whatever this is set to.",
    ),
}

# Stated here so the screen can show it and nobody has to find out by muting
# something and waiting for a failure that never arrives.
ALWAYS_SENT = (
    "Live order approval requests",
    "Rejected or failed orders",
    "A position left without a stop",
    "Blocked reconciliation",
    "Exits that need attention",
    "Refusals on the live path",
)


async def load(session: AsyncSession) -> NotificationSettings:
    """The stored choice, or the defaults.

    A row that no longer validates falls back rather than raising. Losing a
    preference is a nuisance; an exception on the path that is about to send an
    alert would cost the alert.
    """
    stored = await session.get(ApplicationSetting, NOTIFICATION_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return DEFAULTS
    try:
        return NotificationSettings.model_validate({**DEFAULTS.model_dump(), **stored.value})
    except ValueError:
        return DEFAULTS


async def wants(session: AsyncSession, field: str) -> bool:
    """Whether one optional message should be sent. Never raises.

    Defaults to sending. A preference lookup that failed and silenced an alert
    would be the worst of both: the operator believes they are being told, and
    they are not.
    """
    try:
        return bool(getattr(await load(session), field, True))
    except Exception:  # noqa: BLE001 - a preference is not worth losing an alert over
        return True
