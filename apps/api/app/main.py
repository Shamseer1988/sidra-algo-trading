import hmac
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import app.db.models  # noqa: F401 - register declarative models
from app.api.routes import (
    assisted,
    auth,
    backtesting,
    broker,
    events,
    health,
    journal,
    live,
    market_data,
    oms,
    paper,
    risk,
    safety,
    scanner,
    shadow,
    system,
    telegram,
)
from app.api.routes import settings as settings_routes
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.services.oms import reconcile_paper_oms
from app.services.scheduler import check_and_renew_on_startup, init_upstox_scheduler

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger("system")


def security_header_values(app_env: str) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-site",
    }
    if app_env == "production":
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("application.starting", mode=settings.application_mode, live_trading_enabled=False)
    if settings.auto_create_schema and settings.app_env == "development":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.warning("database.schema_auto_created", message="Use Alembic outside local development")
    try:
        async with SessionLocal() as session:
            reconciliation = await reconcile_paper_oms(session)
            await session.commit()
        logger.info(
            "startup.paper_oms_reconciled",
            status=reconciliation.status,
            unknown_orders=reconciliation.unknown_orders,
        )
    except Exception as exc:
        logger.warning("startup.paper_oms_reconciliation_failed", error=str(exc))

    # ── Upstox auto-auth scheduler ───────────────────────────────────
    scheduler = init_upstox_scheduler(settings)
    if scheduler:
        scheduler.start()
        logger.info("startup.scheduler_started", jobs=len(scheduler.get_jobs()))
        # If token is expired/missing, renew immediately on startup
        try:
            await check_and_renew_on_startup(settings)
        except Exception as exc:
            logger.warning("startup.auto_renewal_check_failed", error=str(exc))

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("shutdown.scheduler_stopped")
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
    for header, value in security_header_values(settings.app_env).items():
        response.headers[header] = value
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
app.include_router(risk.router, prefix="/api/v1")
app.include_router(backtesting.router, prefix="/api/v1")
app.include_router(oms.router, prefix="/api/v1")
app.include_router(shadow.router, prefix="/api/v1")
app.include_router(assisted.router, prefix="/api/v1")
app.include_router(live.router, prefix="/api/v1")
