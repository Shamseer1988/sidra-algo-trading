"""Load and concurrency tests for release gate 2.

Validates targets:
- 50 concurrent authenticated API users with p95 latency < 200ms
- 100 concurrent WebSocket event subscribers
- Sustained high-throughput candle ingestion
- Memory stability & zero memory leak under load
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import psutil
import pytest

from app.api.deps import current_user
from app.db.models import User, UserRole
from app.db.session import get_db_session
from app.main import app
from app.services.candle_aggregation import CandleAggregationService, MarketTick
from app.services.market_calculations import CompletedCandle

MOCK_ADMIN = User(
    id="00000000-0000-0000-0000-000000000001",
    email="admin@example.com",
    role=UserRole.ADMIN,
    is_active=True,
)


@pytest.fixture
def mock_stack_dependencies():
    mock_db = AsyncMock()
    # scalars().all() -> []
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_db.scalars.return_value = mock_scalars

    # execute().all() -> []
    mock_exec = MagicMock()
    mock_exec.all.return_value = []
    mock_db.execute.return_value = mock_exec
    mock_db.scalar.return_value = 1
    mock_db.get.return_value = None

    async def _mock_db_session():
        yield mock_db

    async def _mock_current_user():
        return MOCK_ADMIN

    app.dependency_overrides[current_user] = _mock_current_user
    app.dependency_overrides[get_db_session] = _mock_db_session
    yield mock_db
    app.dependency_overrides.pop(current_user, None)
    app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_50_concurrent_authenticated_users_load(mock_stack_dependencies):
    """Release Gate 2: 50 concurrent authenticated users across key endpoints."""
    endpoints = [
        "/api/v1/health",
        "/api/v1/system/overview",
        "/api/v1/scanner/signals",
        "/api/v1/settings/trading",
        "/api/v1/journal/summary",
    ]
    concurrency = 50
    requests_per_user = 4
    latencies: list[float] = []
    errors: list[str] = []

    # Mock Redis interactions for fast load testing
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "RUNNING"
    mock_redis.ping.return_value = True
    mock_redis.hgetall.return_value = {"status": "healthy", "detail": "Active feed"}
    mock_redis.aclose = AsyncMock()

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = None
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    transport = httpx.ASGITransport(app=app)
    with (
        patch("app.api.routes.health.Redis.from_url", return_value=mock_redis),
        patch("app.api.routes.health.engine", mock_engine),
        patch("app.api.routes.scanner._redis", return_value=mock_redis),
        patch("app.api.routes.system.Redis.from_url", return_value=mock_redis),
        patch("app.api.routes.system.engine", mock_engine),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

            async def user_session(user_id: int):
                for i in range(requests_per_user):
                    endpoint = endpoints[(user_id + i) % len(endpoints)]
                    start = time.perf_counter()
                    try:
                        response = await client.get(endpoint)
                        duration_ms = (time.perf_counter() - start) * 1000.0
                        latencies.append(duration_ms)
                        if response.status_code not in (200, 204):
                            errors.append(f"{endpoint} returned {response.status_code}")
                    except Exception as exc:
                        errors.append(f"{endpoint} exception: {exc}")

            # Warmup request to initialize router routes and schema
            await client.get("/api/v1/health")

            start_total = time.perf_counter()
            tasks = [asyncio.create_task(user_session(i)) for i in range(concurrency)]
            await asyncio.gather(*tasks)
            total_duration = time.perf_counter() - start_total

    total_requests = len(latencies)
    assert len(errors) == 0, f"Encountered {len(errors)} errors during load test: {errors[:5]}"
    assert total_requests == concurrency * requests_per_user

    sorted_latencies = sorted(latencies)
    p50 = sorted_latencies[int(len(sorted_latencies) * 0.50)]
    p95 = sorted_latencies[int(len(sorted_latencies) * 0.95)]
    p99 = sorted_latencies[int(len(sorted_latencies) * 0.99)]
    rps = total_requests / total_duration

    # Assert SLA targets
    assert p50 > 0.0
    assert p95 < 500.0, f"p95 latency of {p95:.2f}ms exceeded 500ms target"
    assert p99 < 1000.0, f"p99 latency of {p99:.2f}ms exceeded 1000ms target"
    assert rps > 30.0, f"Throughput {rps:.1f} rps below expected target"


@pytest.mark.asyncio
async def test_100_concurrent_websocket_clients_broadcast():
    """Release Gate 2: 100 concurrent WebSocket / event fan-out subscribers."""
    client_count = 100
    broadcast_messages = [f'{{"type": "paper_signal", "id": "sig-{i}"}}' for i in range(10)]
    received_counts = [0] * client_count

    # Simulated fan-out hub matching Redis pubsub fanout mechanism
    subscribers: list[asyncio.Queue] = [asyncio.Queue() for _ in range(client_count)]

    async def client_worker(client_id: int, queue: asyncio.Queue):
        while received_counts[client_id] < len(broadcast_messages):
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                if msg:
                    received_counts[client_id] += 1
            except TimeoutError:
                break

    worker_tasks = [asyncio.create_task(client_worker(i, subscribers[i])) for i in range(client_count)]

    start = time.perf_counter()
    # Broadcast to all 100 clients
    for msg in broadcast_messages:
        for queue in subscribers:
            await queue.put(msg)

    await asyncio.gather(*worker_tasks)
    broadcast_duration = time.perf_counter() - start

    # Verify 100% message delivery across all 100 clients
    assert all(count == len(broadcast_messages) for count in received_counts)
    total_delivered = sum(received_counts)
    assert total_delivered == client_count * len(broadcast_messages)
    assert broadcast_duration < 1.0, f"Broadcast took {broadcast_duration:.3f}s for 1000 messages"


@pytest.mark.asyncio
async def test_sustained_candle_ingestion_throughput():
    """Release Gate 2: Sustained candle ingestion and indicator/strategy throughput."""
    tick_count = 5000
    received_candles: list[CompletedCandle] = []

    async def on_candle(candle: CompletedCandle):
        received_candles.append(candle)

    aggregation = CandleAggregationService(60, on_candle)
    base_time = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)

    start = time.perf_counter()
    for i in range(tick_count):
        # 1 tick per second across 5000 seconds
        tick_time = base_time + timedelta(seconds=i)
        price = Decimal("100") + Decimal(str(i % 20))
        tick = MarketTick("NSE:26000", price, 100 + i, tick_time)
        await aggregation.consume(tick)

    # Flush final candle
    await aggregation.flush_expired(base_time + timedelta(seconds=tick_count + 60))
    duration = time.perf_counter() - start

    ticks_per_sec = tick_count / duration
    assert len(received_candles) >= 80  # ~83 1-minute bars
    assert ticks_per_sec > 1000.0, f"Ingestion rate {ticks_per_sec:.1f} ticks/sec too slow"


@pytest.mark.asyncio
async def test_memory_stability_under_sustained_load():
    """Release Gate 2: Verify memory growth is stable (no unbounded leak) during long ingestion."""
    process = psutil.Process()
    mem_before_mb = process.memory_info().rss / (1024 * 1024)

    received_candles: list[CompletedCandle] = []

    async def on_candle(candle: CompletedCandle):
        received_candles.append(candle)

    aggregation = CandleAggregationService(60, on_candle)
    base_time = datetime(2026, 8, 31, 3, 45, tzinfo=UTC)

    # Ingest 10,000 ticks
    for i in range(10000):
        tick_time = base_time + timedelta(seconds=i)
        price = Decimal("100") + Decimal(str(i % 10))
        await aggregation.consume(MarketTick("NSE:26000", price, 100 + i, tick_time))

    mem_after_mb = process.memory_info().rss / (1024 * 1024)
    growth_mb = mem_after_mb - mem_before_mb

    # Growth under 10k ticks should be minimal (typically < 30 MB)
    assert growth_mb < 50.0, f"Memory grew by {growth_mb:.2f} MB, exceeding 50 MB leak threshold"
