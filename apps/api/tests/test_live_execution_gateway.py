"""The one place a broker client capable of placing an order is built.

The property worth testing is narrow and load-bearing: the broker comes from the
operator's selection, and NONE is a refusal rather than a hint to pick whichever
broker happens to have credentials. A gateway that guessed would send a real
order to a broker nobody chose, and the operator would find out from a contract
note rather than from this system.

The second property is the read/write split. Reconciliation and recovery are
handed something with no placement method at all, so they cannot place an order
however they are later edited — which matters most during recovery, where the
thing being resolved is an order that may already exist.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.api.routes.settings import TRADING_KEY
from app.db.models import ApplicationSetting
from app.db.session import SessionLocal
from app.services.firstock.client import FirstockSession
from app.services.live_execution_gateway import (
    BrokerNotSelectedError,
    live_order_adapter,
    live_report_adapter,
    selected_live_broker,
)

CONTROLS = {
    "account_capital": 10000,
    "risk_per_trade_percent": 1,
    "maximum_daily_risk_percent": 3,
    "maximum_daily_trades": 4,
    "minimum_score": 80,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
}


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        firstock_is_configured=True,
        firstock_rate_limit_per_second=8.0,
        upstox_token_encryption_key=None,
        upstox_access_token="an-access-token",
    )


class FakeFirstockClient:
    def __init__(self, _settings: object) -> None:
        pass

    async def login(self) -> FirstockSession:
        return FirstockSession(user_id="user", session_token="jkey")


@pytest.fixture(autouse=True)
def firstock_login(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.firstock.client.FirstockClient", FakeFirstockClient)
    yield


async def set_broker(session, broker: str) -> None:  # noqa: ANN001
    await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
    session.add(ApplicationSetting(key=TRADING_KEY, value={**CONTROLS, "live_broker": broker}))
    await session.commit()


async def clear_broker(session) -> None:  # noqa: ANN001
    await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
    await session.commit()


# --- the selection --------------------------------------------------------


async def test_the_default_is_no_broker_at_all() -> None:
    """Shipping with a broker pre-selected is how an order goes somewhere unintended."""
    async with SessionLocal() as session:
        await clear_broker(session)
        assert await selected_live_broker(session) == "NONE"


@pytest.mark.parametrize("broker", ["UPSTOX", "FIRSTOCK"])
async def test_the_selection_decides_which_adapter_is_built(broker: str) -> None:
    async with SessionLocal() as session:
        await set_broker(session, broker)
        try:
            adapter = await live_order_adapter(settings(), session)
            assert adapter.name == broker
        finally:
            await clear_broker(session)


@pytest.mark.parametrize("broker", ["NONE", "", "KOTAK"])
async def test_an_unselected_or_unknown_broker_refuses_rather_than_defaulting(broker: str) -> None:
    """Credentials being present is not the same as somebody having chosen."""
    async with SessionLocal() as session:
        await clear_broker(session)
        with pytest.raises(BrokerNotSelectedError):
            await live_order_adapter(settings(), session, broker)


async def test_an_explicit_broker_overrides_the_current_selection() -> None:
    """Recovery resolves an order against the broker it was sent to.

    Looking for it in the currently-selected broker's book would find nothing
    and escalate an order that is sitting there plainly visible.
    """
    async with SessionLocal() as session:
        await set_broker(session, "UPSTOX")
        try:
            adapter = await live_report_adapter(settings(), session, "FIRSTOCK")
            assert adapter.name == "FIRSTOCK"
        finally:
            await clear_broker(session)


# --- the read/write split -------------------------------------------------


@pytest.mark.parametrize("broker", ["UPSTOX", "FIRSTOCK"])
async def test_a_report_adapter_holds_a_client_that_cannot_place_an_order(broker: str) -> None:
    async with SessionLocal() as session:
        await set_broker(session, broker)
        try:
            adapter = await live_report_adapter(settings(), session)
            assert not hasattr(adapter._client, "place_order")
        finally:
            await clear_broker(session)


@pytest.mark.parametrize("broker", ["UPSTOX", "FIRSTOCK"])
async def test_an_order_adapter_holds_a_client_that_can(broker: str) -> None:
    async with SessionLocal() as session:
        await set_broker(session, broker)
        try:
            adapter = await live_order_adapter(settings(), session)
            assert hasattr(adapter._client, "place_order")
        finally:
            await clear_broker(session)


# --- missing credentials --------------------------------------------------


async def test_firstock_without_credentials_refuses() -> None:
    async with SessionLocal() as session:
        await set_broker(session, "FIRSTOCK")
        try:
            broken = settings()
            broken.firstock_is_configured = False
            with pytest.raises(BrokerNotSelectedError, match="credentials"):
                await live_order_adapter(broken, session)
        finally:
            await clear_broker(session)


async def test_upstox_without_a_stored_token_refuses() -> None:
    """An expired authorisation must read as "authorise again", not as an outage."""
    async with SessionLocal() as session:
        await set_broker(session, "UPSTOX")
        try:
            broken = settings()
            broken.upstox_access_token = None
            with pytest.raises(BrokerNotSelectedError, match="access token"):
                await live_order_adapter(broken, session)
        finally:
            await clear_broker(session)
