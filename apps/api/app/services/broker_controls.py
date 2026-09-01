"""Persisted, non-secret market-data connector selection."""

import json
from uuid import UUID

from pydantic import BaseModel, model_validator
from redis.asyncio import Redis

from app.db.models import ApplicationSetting
from app.db.session import SessionLocal

BROKER_CONTROLS_KEY = "broker_controls"
BROKER_CONTROLS_REDIS_KEY = "market:broker_controls"


class BrokerControls(BaseModel):
    upstox_paper_enabled: bool = True
    firstock_feed_enabled: bool = False

    @model_validator(mode="after")
    def one_market_feed_at_a_time(self) -> "BrokerControls":
        if self.upstox_paper_enabled and self.firstock_feed_enabled:
            raise ValueError("Only one market-data connector can be enabled at a time")
        return self

    @property
    def active_broker(self) -> str:
        if self.upstox_paper_enabled:
            return "UPSTOX"
        if self.firstock_feed_enabled:
            return "FIRSTOCK"
        return "NONE"


async def load_broker_controls(redis: Redis) -> BrokerControls:
    raw = await redis.get(BROKER_CONTROLS_REDIS_KEY)
    if raw:
        try:
            return BrokerControls.model_validate_json(raw)
        except ValueError:
            pass
    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, BROKER_CONTROLS_KEY)
        controls = BrokerControls.model_validate(setting.value if setting else {})
    await redis.set(BROKER_CONTROLS_REDIS_KEY, controls.model_dump_json())
    return controls


async def save_broker_controls(
    redis: Redis,
    controls: BrokerControls,
    updated_by_user_id: UUID | None = None,
) -> None:
    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, BROKER_CONTROLS_KEY)
        if setting is None:
            setting = ApplicationSetting(
                key=BROKER_CONTROLS_KEY,
                value=controls.model_dump(),
                updated_by_user_id=updated_by_user_id,
            )
            session.add(setting)
        else:
            setting.value = controls.model_dump()
            setting.updated_by_user_id = updated_by_user_id
        await session.commit()
    await redis.set(BROKER_CONTROLS_REDIS_KEY, json.dumps(controls.model_dump()))
