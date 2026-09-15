"""Build a broker client capable of submission.

One function, in its own module, for one reason: it is the single place where a
``FirstockOrderClient`` — the only client that can place an order — comes into
existence. Anything that can submit has to go through here, which makes the
question "what in this system can reach a broker with intent" answerable by
looking at who imports this module.

A fresh login per call is deliberate. The paths that need submission are
human-paced — an operator approving one order — so there is nothing to gain from
caching, and a token obtained moments before use is one fewer thing that can be
stale at the moment it matters. The shadow evaluator, which runs per signal,
caches its own read-only session separately.
"""

from app.core.config import Settings
from app.services.firstock.client import FirstockClient, FirstockError
from app.services.firstock.orders import FirstockOrderClient


async def live_order_client(settings: Settings) -> FirstockOrderClient:
    """Authenticate and return a submission-capable client.

    Raises rather than returning None. A caller that forgot to check a None
    would proceed as though it had a client; a raise cannot be ignored by
    accident.
    """
    if not settings.firstock_is_configured:
        raise FirstockError("Firstock credentials are not configured")
    session = await FirstockClient(settings).login()
    return FirstockOrderClient(settings, session)
