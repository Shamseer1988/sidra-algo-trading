from datetime import UTC, datetime

from redis.asyncio import Redis

EMERGENCY_STOP_KEY = "safety:emergency_stop"
PAPER_TRACKING_KEY = "safety:paper_tracking_enabled"
SCANNER_CONTROL_KEY = "scanner:control_state"


async def emergency_stop(redis: Redis, reason: str, source: str) -> None:
    await redis.hset(
        EMERGENCY_STOP_KEY,
        mapping={"active": "true", "reason": reason, "source": source, "at": datetime.now(UTC).isoformat()},
    )
    await redis.set(SCANNER_CONTROL_KEY, "STOPPED")


async def clear_emergency_stop(redis: Redis) -> None:
    await redis.delete(EMERGENCY_STOP_KEY)


async def emergency_stop_state(redis: Redis) -> dict[str, str]:
    return await redis.hgetall(EMERGENCY_STOP_KEY)


async def paper_tracking_enabled(redis: Redis) -> bool:
    value = await redis.get(PAPER_TRACKING_KEY)
    return value != "false"
