from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, health, system
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.session import engine
import app.db.models  # noqa: F401 - register declarative models

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger("system")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("application.starting", mode=settings.application_mode, live_trading_enabled=False)
    if settings.auto_create_schema and settings.app_env == "development":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.warning("database.schema_auto_created", message="Use Alembic outside local development")
    yield
    await engine.dispose()
    logger.info("application.stopped")


app = FastAPI(
    title="Intraday Sentinel API",
    version="0.1.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(settings.web_origin).rstrip("/")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
