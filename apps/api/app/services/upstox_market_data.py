"""Upstox V3 market-data adapter for PAPER scanning only.

It uses Upstox's official Python SDK to decode its Protobuf WebSocket feed.  This
module intentionally exposes no trading, portfolio, or order operation.
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.config import Settings
from app.services.candle_aggregation import MarketTick, normalize_market_timestamp
from app.services.firstock.market_data import CONNECTION_STATE_KEY, LAST_TICK_KEY


def configured_subscriptions(settings: Settings) -> list[str]:
    return [value.strip() for value in settings.upstox_subscriptions.split(",") if value.strip()]


def _number(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _volume(feed: dict[str, Any]) -> int | None:
    details = feed.get("eFeedDetails") if isinstance(feed.get("eFeedDetails"), dict) else {}
    value = details.get("vtt") or feed.get("vtt")
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _ltpc(feed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(feed.get("ltpc"), dict):
        return feed["ltpc"], feed
    full = (
        feed.get("fullFeed")
        if isinstance(feed.get("fullFeed"), dict)
        else feed.get("ff")
        if isinstance(feed.get("ff"), dict)
        else {}
    )
    market = (
        full.get("marketFF")
        if isinstance(full.get("marketFF"), dict)
        else full.get("indexFF")
        if isinstance(full.get("indexFF"), dict)
        else full
    )
    return market.get("ltpc", {}) if isinstance(market.get("ltpc"), dict) else {}, market


class UpstoxMarketDataService:
    """Official-SDK Upstox V3 feed, normalized into the shared candle pipeline."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        on_tick: Callable[[MarketTick], Awaitable[None]],
        access_token: str | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._on_tick = on_tick
        self._access_token = access_token or settings.upstox_access_token
        self._logger = structlog.get_logger("upstox.market_data")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._streamer: Any | None = None

    async def _state(self, status: str, detail: str) -> None:
        await self._redis.hset(
            CONNECTION_STATE_KEY,
            mapping={
                "status": status,
                "detail": detail,
                "updated_at": datetime.now(UTC).isoformat(),
                "provider": "UPSTOX",
            },
        )

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        feeds = payload.get("feeds")
        if not isinstance(feeds, dict):
            return
        received_at = datetime.now(UTC)
        normalized_ticks: list[MarketTick] = []
        pipe = self._redis.pipeline()
        for instrument_key, raw_feed in feeds.items():
            if not isinstance(instrument_key, str) or not isinstance(raw_feed, dict):
                continue
            ltpc, full_feed = _ltpc(raw_feed)
            price = _number(ltpc.get("ltp"))
            if price is None:
                continue
            volume = _volume(full_feed)
            exchange_timestamp = normalize_market_timestamp(ltpc.get("ltt") or payload.get("currentTs"), received_at)
            pipe.set(
                f"market:tick:{instrument_key}",
                json.dumps(
                    {
                        "instrument_token": instrument_key,
                        "price": str(price),
                        "volume": volume,
                        "exchange_timestamp": exchange_timestamp.isoformat(),
                        "received_timestamp": received_at.isoformat(),
                        "latency_ms": max(int((received_at - exchange_timestamp).total_seconds() * 1000), 0),
                        "provider": "UPSTOX",
                    }
                ),
                ex=90,
            )
            normalized_ticks.append(
                MarketTick(
                    instrument_token=instrument_key,
                    price=price,
                    cumulative_volume=volume,
                    exchange_timestamp=exchange_timestamp,
                    received_timestamp=received_at,
                )
            )
        if normalized_ticks:
            pipe.set(LAST_TICK_KEY, received_at.isoformat(), ex=90)
            await pipe.execute()
            for tick in normalized_ticks:
                await self._on_tick(tick)

    def _on_message(self, payload: dict[str, Any]) -> None:
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._handle_message(payload), self._loop)

    def _on_open(self) -> None:
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._state(
                    "LIVE", f"Receiving Upstox data for {len(configured_subscriptions(self._settings))} instruments"
                ),
                self._loop,
            )

    async def run_forever(self) -> None:
        if not self._access_token or not configured_subscriptions(self._settings):
            await self._state("NOT_CONFIGURED", "Set Upstox access token and confirmed instrument keys")
            return
        try:
            import upstox_client
        except ImportError:
            await self._state("DEGRADED", "Upstox SDK is unavailable in this build")
            return
        self._loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _on_close(*args: Any) -> None:
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(stop_event.set)

        def _on_error(error: Any) -> None:
            self._logger.warning("upstox.streamer_error", error=str(error))
            if "401" in str(error) and self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(stop_event.set)

        configuration = upstox_client.Configuration()
        configuration.access_token = self._access_token
        self._streamer = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(configuration), configured_subscriptions(self._settings), "full"
        )
        self._streamer.on("open", self._on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("close", _on_close)
        self._streamer.on("error", _on_error)
        self._streamer.on("autoReconnectStopped", _on_close)
        await self._state("CONNECTING", "Opening Upstox Market Data Feed V3")
        try:
            self._streamer.connect()
            await stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.warning("upstox.connection_ended")
            await self._state("DEGRADED", "Upstox market-data connection ended")
        finally:
            if self._streamer:
                with contextlib.suppress(Exception):
                    self._streamer.disconnect()
            self._streamer = None
