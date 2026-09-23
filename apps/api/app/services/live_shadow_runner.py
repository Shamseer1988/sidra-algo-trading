"""Drive live shadow evaluation from the paper execution path.

Separated from ``live_shadow`` because the evaluator should stay a pure function
of its inputs — session, settings, adapter, signal — while this module owns the
awkward parts: is the feature on, has an operator chosen a broker, do we still
hold a usable login, and which approval mode is currently selected.

Everything here is best-effort by design. Paper execution is the system the
operator's results come from; shadow evidence is worth collecting only for as
long as gathering it cannot cost a paper trade. So the entry point returns None
on every failure and logs, rather than propagating.

The broker client is cached, per broker. A login per signal would spend an API
call for no information and would multiply the account's authentication traffic
by the signal rate for no reason; caching it to a short TTL keeps a stale token
from quietly poisoning a whole session's evidence. It is keyed by broker so that
changing the selection takes effect on the next signal rather than whenever the
old broker's TTL happens to lapse.

Only read-only clients are cached here, and that is not incidental: a cache of
submission-capable clients in the path that runs on every signal is exactly the
thing this layer is built to make impossible.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.core.config import Settings
from app.db.models import ApplicationSetting, PaperSignal
from app.db.session import SessionLocal
from app.services.broker_adapter import (
    BROKER_FIRSTOCK,
    BROKER_UPSTOX,
    BrokerAdapter,
    FirstockAdapter,
    UpstoxAdapter,
)
from app.services.firstock.client import FirstockError
from app.services.live_execution_gateway import BrokerNotSelectedError, selected_live_broker
from app.services.live_shadow import shadow_paper_signal
from app.services.upstox_orders import UpstoxError

logger = logging.getLogger(__name__)

# Imported eagerly, unlike the clients themselves, because they are needed in an
# except clause and a lazy import inside one is a second failure waiting to
# happen.
_AUTH_FAILURES = (BrokerNotSelectedError, FirstockError, UpstoxError)


@dataclass
class _CachedClient:
    broker: str
    client: Any
    obtained_at: datetime


_cached_client: _CachedClient | None = None


def reset_cached_login() -> None:
    """Drop the cached broker client. Used by tests and by credential changes."""
    global _cached_client
    _cached_client = None


async def _report_client(settings: Settings, broker: str) -> Any:  # noqa: ANN401
    """A read-only client for the selected broker, reused within its TTL."""
    global _cached_client
    ttl = timedelta(minutes=settings.live_shadow_session_ttl_minutes)
    now = datetime.now(UTC)
    if _cached_client is not None and _cached_client.broker == broker and now - _cached_client.obtained_at < ttl:
        return _cached_client.client

    if broker == BROKER_UPSTOX:
        from app.services.upstox_oauth import load_access_token
        from app.services.upstox_orders import UpstoxReportClient, UpstoxSession

        token = await load_access_token(settings)
        if not token:
            raise BrokerNotSelectedError("Upstox has no stored access token")
        client: Any = UpstoxReportClient(settings, UpstoxSession(access_token=token))
    elif broker == BROKER_FIRSTOCK:
        from app.services.firstock.client import FirstockClient
        from app.services.firstock.orders import FirstockReportClient

        if not settings.firstock_is_configured:
            raise BrokerNotSelectedError("Firstock credentials are not configured")
        client = FirstockReportClient(settings, await FirstockClient(settings).login())
    else:
        raise BrokerNotSelectedError(f"Broker {broker} has no adapter")

    _cached_client = _CachedClient(broker=broker, client=client, obtained_at=now)
    return client


def _adapter_for(broker: str, client: Any, session: Any) -> BrokerAdapter:  # noqa: ANN401
    if broker == BROKER_UPSTOX:
        return UpstoxAdapter(client)
    return FirstockAdapter(client, session)


async def _approval_mode_and_leverage(session) -> tuple[str, bool]:  # noqa: ANN001
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    return controls.execution_approval_mode, bool(controls.intraday_leverage_enabled)


async def run_live_shadow(settings: Settings, signal: PaperSignal, oms_order_id) -> None:  # noqa: ANN001
    """Evaluate one paper signal against the live path. Never raises.

    Opens its own database session deliberately: the caller has just committed a
    paper order, and holding that transaction open across a broker login and a
    margin call would turn a network stall into a lock held over the paper
    ledger.
    """
    if not settings.live_shadow_enabled:
        return

    try:
        async with SessionLocal() as lookup:
            broker = await selected_live_broker(lookup)
    except Exception:
        logger.exception("Live shadow could not read the selected broker")
        return

    if broker not in {BROKER_UPSTOX, BROKER_FIRSTOCK}:
        logger.debug("Live shadow is enabled but no broker is selected; skipping signal %s", signal.id)
        return

    try:
        client = await _report_client(settings, broker)
    except _AUTH_FAILURES as exc:
        # Not evidence about this signal, so nothing is recorded: a row saying
        # "we could not log in" repeated for every signal would drown the
        # refusals that describe the strategy. Logged at warning rather than as
        # an exception because a broker being unreachable is an outage, not a
        # bug, and a stack trace per signal buries the ones that are.
        reset_cached_login()
        logger.warning("Live shadow could not authenticate with %s: %s", broker, exc)
        return
    except Exception:
        reset_cached_login()
        logger.exception("Live shadow login raised unexpectedly")
        return

    try:
        async with SessionLocal() as session:
            approval_mode, leverage_enabled = await _approval_mode_and_leverage(session)
            attached = await session.get(PaperSignal, signal.id)
            if attached is None:
                return
            await shadow_paper_signal(
                session,
                settings,
                _adapter_for(broker, client, session),
                signal=attached,
                oms_order_id=oms_order_id,
                approval_mode=approval_mode,
                intraday_leverage_enabled=leverage_enabled,
            )
            await session.commit()
    except Exception:
        logger.exception("Live shadow evaluation failed for paper signal %s", signal.id)
