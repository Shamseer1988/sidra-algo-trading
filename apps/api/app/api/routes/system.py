from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(prefix="/system", tags=["System"])


class VersionResponse(BaseModel):
    application: str
    version: str
    mode: str
    live_trading_enabled: bool


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    settings = get_settings()
    return VersionResponse(
        application="Intraday Sentinel",
        version="0.1.0",
        mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
    )
