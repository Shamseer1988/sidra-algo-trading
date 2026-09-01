import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import structlog
import websockets
from redis.asyncio import Redis
from websockets.exceptions import WebSocketException

from app.core.config import Settings
from app.services.candle_aggregation import MarketTick
from app.services.firstock.client import FirstockClient, FirstockError, FirstockSession

FIRSTOCK_WS_URL = "wss://socket.firstock.in/V2/ws"
CONNECTION_STATE_KEY = "firstock:connection_state"
LAST_TICK_KEY = "firstock:last_tick_at"


def configured_subscriptions(settings: Settings) -> list[str]:
    return [token.strip() for token in settings.firstock_subscriptions.split("|") if token.strip()]


def parse_price(value: object) -> str | None:
    """Firstock V2 feed values are paise; retain precision as a decimal string."""
    try:
        return str(Decimal(str(value)) / Decimal("100"))
    except (InvalidOperation, ValueError):
        return None


def parse_volume(value: object) -> int | None:
    try:
        parsed = int(Decimal(str(value)))
        return parsed if parsed >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class FirstockMarketDataService:
    """Maintains only market data. It has no order- or position-execution capability."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        on_tick: Callable[[MarketTick], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._on_tick = on_tick
        self._logger = structlog.get_logger("firstock.market_data")

    async def _state(self, status: str, detail: str) -> None:
        await self._redis.hset(
            CONNECTION_STATE_KEY,
            mapping={
                "status": status,
                "detail": detail,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def _subscribe(self, websocket: Any) -> None:
        tokens = configured_subscriptions(self._settings)
        if not tokens:
            await self._state("CONNECTED", "Authenticated; no configured market-data subscriptions")
            return
        await websocket.send(json.dumps({"action": "subscribe", "tokens": "|".join(tokens)}))
        await self._state("LIVE", f"Receiving market data for {len(tokens)} configured instruments")

    async def _handle_message(self, raw_message: str | bytes) -> None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            self._logger.warning("firstock.invalid_message")
            return
        if not isinstance(payload, dict):
            return
        received_datetime = datetime.now(UTC)
        received_at = received_datetime.isoformat()
        pipe = self._redis.pipeline()
        tick_count = 0
        market_ticks: list[MarketTick] = []
        for instrument_token, tick in payload.items():
            if not isinstance(instrument_token, str) or not isinstance(tick, dict):
                continue
            price = parse_price(tick.get("i_last_traded_price"))
            if price is None:
                continue
            volume = parse_volume(tick.get("i_volume_traded_today"))
            normalized = {
                "instrument_token": instrument_token,
                "price": price,
                "volume": volume,
                "exchange_feed_time": tick.get("i_feed_time"),
                "received_at": received_at,
            }
            pipe.set(f"market:tick:{instrument_token}", json.dumps(normalized), ex=90)
            market_ticks.append(
                MarketTick(
                    instrument_token=instrument_token,
                    price=Decimal(price),
                    cumulative_volume=volume,
                    occurred_at=received_datetime,
                )
            )
            tick_count += 1
        if tick_count:
            pipe.set(LAST_TICK_KEY, received_at, ex=90)
            await pipe.execute()
            if self._on_tick:
                for tick in market_ticks:
                    await self._on_tick(tick)

    async def _connect(self, session: FirstockSession) -> None:
        query = urlencode({"userId": session.user_id, "jKey": session.session_token, "source": "developer-api"})
        await self._state("CONNECTING", "Opening Firstock WebSocket V2 market-feed connection")
        async with websockets.connect(
            f"{FIRSTOCK_WS_URL}?{query}", ping_interval=20, ping_timeout=10, close_timeout=10
        ) as websocket:
            await self._state("CONNECTED", "Firstock WebSocket V2 authenticated")
            await self._subscribe(websocket)
            async for message in websocket:
                await self._handle_message(message)

    async def run_forever(self) -> None:
        if not self._settings.firstock_is_configured:
            await self._state("NOT_CONFIGURED", "Firstock credentials are not configured")
            return
        delay_seconds = 2
        while True:
            try:
                session = await FirstockClient(self._settings).login()
                await self._connect(session)
                delay_seconds = 2
            except asyncio.CancelledError:
                await self._state("STOPPED", "Market-data worker stopped")
                raise
            except (FirstockError, WebSocketException, OSError):
                await self._state("DEGRADED", "Firstock connection failed; retrying with backoff")
                self._logger.warning("firstock.connection_retry", delay_seconds=delay_seconds)
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 60)
