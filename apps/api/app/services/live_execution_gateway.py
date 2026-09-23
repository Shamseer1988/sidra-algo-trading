"""Build the broker adapter a live order would travel through.

Two functions, in their own module, for one reason: this is the single place
where a client capable of placing an order comes into existence. Anything that
can submit has to go through here, which makes the question "what in this system
can reach a broker with intent" answerable by looking at who imports this module.

**The broker is chosen from the database, not from configuration.** An operator
picks it in admin settings, and the default is ``NONE`` — which is a refusal, not
a fallback. A gateway that quietly defaulted to whichever broker had credentials
would send an order to a broker nobody selected, and the operator would find out
from a contract note.

``live_order_adapter`` can place orders; ``live_report_adapter`` cannot. They are
separate so that reconciliation, recovery and the shadow evaluator — all of which
only read — hold something that has no ``submit`` at all, rather than holding a
submitting client and being trusted not to use it.

A fresh login per call is deliberate. The paths that need submission are
human-paced — an operator approving one order — so there is nothing to gain from
caching, and a token obtained moments before use is one fewer thing that can be
stale at the moment it matters. The shadow evaluator, which runs per signal,
caches its own read-only session separately.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApplicationSetting
from app.services.broker_adapter import (
    BROKER_FIRSTOCK,
    BROKER_UPSTOX,
    SUPPORTED_BROKERS,
    BrokerAdapter,
    FirstockAdapter,
    UpstoxAdapter,
)


class BrokerNotSelectedError(RuntimeError):
    """No broker is selected, or the selected one cannot be reached.

    A distinct type so callers can tell "nobody has chosen a broker" apart from
    "the broker refused us", and so neither can be mistaken for a submission
    that failed at the exchange.
    """


async def selected_live_broker(session: AsyncSession) -> str:
    """Which broker an operator selected, from the trading controls.

    Read here rather than passed in, so that every submission path resolves it
    the same way and a caller cannot supply a broker the operator did not pick.
    """
    # Imported inside the function: the settings route imports the live modules
    # for its own types, and a module-level import would close the cycle.
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

    setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    return controls.live_broker


def _require_supported(broker: str) -> str:
    normalized = (broker or "").strip().upper()
    if normalized not in SUPPORTED_BROKERS:
        raise BrokerNotSelectedError(
            f"No live broker is selected (live_broker is {normalized or 'unset'}). "
            "Choose one in admin settings before anything can be sent."
        )
    return normalized


async def _upstox_client(settings: Settings, *, submitting: bool):  # noqa: ANN202
    from app.services.upstox_oauth import load_access_token
    from app.services.upstox_orders import UpstoxOrderClient, UpstoxReportClient, UpstoxSession

    token = await load_access_token(settings)
    if not token:
        raise BrokerNotSelectedError("Upstox has no stored access token; authorise the app before trading.")
    broker_session = UpstoxSession(access_token=token)
    factory = UpstoxOrderClient if submitting else UpstoxReportClient
    return factory(settings, broker_session)


async def _firstock_client(settings: Settings, *, submitting: bool):  # noqa: ANN202
    from app.services.firstock.client import FirstockClient
    from app.services.firstock.orders import FirstockOrderClient, FirstockReportClient

    if not settings.firstock_is_configured:
        raise BrokerNotSelectedError("Firstock credentials are not configured")
    broker_session = await FirstockClient(settings).login()
    factory = FirstockOrderClient if submitting else FirstockReportClient
    return factory(settings, broker_session)


async def _adapter(
    settings: Settings,
    session: AsyncSession,
    broker: str | None,
    *,
    submitting: bool,
) -> BrokerAdapter:
    name = _require_supported(broker if broker is not None else await selected_live_broker(session))
    if name == BROKER_UPSTOX:
        return UpstoxAdapter(await _upstox_client(settings, submitting=submitting))
    if name == BROKER_FIRSTOCK:
        return FirstockAdapter(await _firstock_client(settings, submitting=submitting), session)
    # Unreachable while SUPPORTED_BROKERS and this branch agree. Kept so that
    # adding a broker to the set without adding it here refuses rather than
    # falling through to whichever adapter happened to be last.
    raise BrokerNotSelectedError(f"Broker {name} has no adapter")


async def live_order_adapter(
    settings: Settings,
    session: AsyncSession,
    broker: str | None = None,
) -> BrokerAdapter:
    """Authenticate and return a submission-capable adapter.

    Raises rather than returning None. A caller that forgot to check a None
    would proceed as though it had an adapter; a raise cannot be ignored by
    accident.
    """
    return await _adapter(settings, session, broker, submitting=True)


async def live_report_adapter(
    settings: Settings,
    session: AsyncSession,
    broker: str | None = None,
) -> BrokerAdapter:
    """Authenticate and return a read-only adapter.

    The client underneath has no placement method at all, so reconciliation and
    recovery cannot place an order however they are edited. That matters most
    during recovery, where the thing being resolved is an order that may already
    exist: code that could send one there turns an uncertain order into two.
    """
    return await _adapter(settings, session, broker, submitting=False)
