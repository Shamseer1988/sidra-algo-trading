"""Drive live shadow evaluation from the paper execution path.

Separated from ``live_shadow`` because the evaluator should stay a pure function
of its inputs — session, settings, client, signal — while this module owns the
awkward parts: is the feature on, is a broker configured, do we still hold a
usable login, and which approval mode is currently selected.

Everything here is best-effort by design. Paper execution is the system the
operator's results come from; shadow evidence is worth collecting only for as
long as gathering it cannot cost a paper trade. So the entry point returns None
on every failure and logs, rather than propagating.

The Firstock login is cached. A login per signal would spend an API call for no
information and would multiply the account's authentication traffic by the
signal rate for no reason; caching it to a short TTL keeps a stale token from
quietly poisoning a whole session's evidence.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.core.config import Settings
from app.db.models import ApplicationSetting, PaperSignal
from app.db.session import SessionLocal
from app.services.firstock.client import FirstockClient, FirstockError, FirstockSession
from app.services.firstock.orders import FirstockReportClient
from app.services.live_shadow import shadow_paper_signal

logger = logging.getLogger(__name__)


@dataclass
class _CachedLogin:
    session: FirstockSession
    obtained_at: datetime


_cached_login: _CachedLogin | None = None


def reset_cached_login() -> None:
    """Drop the cached broker login. Used by tests and by credential changes."""
    global _cached_login
    _cached_login = None


async def _broker_session(settings: Settings) -> FirstockSession:
    global _cached_login
    ttl = timedelta(minutes=settings.live_shadow_session_ttl_minutes)
    now = datetime.now(UTC)
    if _cached_login is not None and now - _cached_login.obtained_at < ttl:
        return _cached_login.session
    session = await FirstockClient(settings).login()
    _cached_login = _CachedLogin(session=session, obtained_at=now)
    return session


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
    if not settings.firstock_is_configured:
        logger.debug("Live shadow is enabled but Firstock is not configured; skipping signal %s", signal.id)
        return

    try:
        broker_session = await _broker_session(settings)
    except FirstockError as exc:
        # A failed login is not evidence about this signal, so nothing is
        # recorded: a row saying "we could not log in" repeated for every signal
        # would drown the refusals that describe the strategy.
        reset_cached_login()
        logger.warning("Live shadow could not authenticate with Firstock: %s", exc)
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
                FirstockReportClient(settings, broker_session),
                signal=attached,
                oms_order_id=oms_order_id,
                approval_mode=approval_mode,
                intraday_leverage_enabled=leverage_enabled,
            )
            await session.commit()
    except Exception:
        logger.exception("Live shadow evaluation failed for paper signal %s", signal.id)
