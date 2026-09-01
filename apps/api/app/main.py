import hmac
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import app.db.models  # noqa: F401 - register declarative models
from app.api.routes import auth, broker, events, health, journal, market_data, paper, safety, scanner, system, telegram
from app.api.routes import settings as settings_routes
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.session import engine

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


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {
        "/api/v1/auth/login",
        "/api/v1/telegram/webhook",
    }:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("X-CSRF-Token")
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": "CSRF validation failed"})
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(scanner.router, prefix="/api/v1")
app.include_router(settings_routes.router, prefix="/api/v1")
app.include_router(broker.router, prefix="/api/v1")
app.include_router(safety.router, prefix="/api/v1")
app.include_router(telegram.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(market_data.router, prefix="/api/v1")
app.include_router(journal.router, prefix="/api/v1")
app.include_router(paper.router, prefix="/api/v1")
