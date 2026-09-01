import os
import socket
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def _resolved_db_url(url: str) -> str:
    if "@postgres:" in url:
        try:
            socket.gethostbyname("postgres")
        except OSError:
            host_port = os.getenv("HOST_POSTGRES_PORT", "5433")
            return url.replace("@postgres:5432", f"@127.0.0.1:{host_port}").replace(
                "@postgres:", f"@127.0.0.1:{host_port}/"
            )
    return url


settings = get_settings()
engine = create_async_engine(
    _resolved_db_url(str(settings.database_url)), pool_pre_ping=True, pool_size=10, max_overflow=10
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
