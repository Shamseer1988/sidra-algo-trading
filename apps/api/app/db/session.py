"""The engine, and the one decision made before it exists.

``DATABASE_URL`` names ``postgres``, the compose service. That name resolves
inside the compose network and nowhere else, so a script run straight on the
NAS cannot use it -- which is why there is a fallback to the published port on
127.0.0.1. The fallback is for the host. Inside a container it is never right:
127.0.0.1 is the container itself, and nothing is listening there.

On 29 October the NAS restarted. Docker brought every container back under its
restart policy, which -- unlike ``docker compose up`` -- does not honour
``depends_on``, so the scanner worker started before DNS could answer for
``postgres``. The lookup failed once, the fallback rewrote the URL to
127.0.0.1:5433, the engine was built from it at import, and nothing ever
reconsidered. The worker then logged the same connection error every thirty
seconds for four days. It stored no candles, so the scanner had nothing to
evaluate and placed no orders on 29 and 30 October or 2 November, and the
failure was found only because somebody noticed the absence of trades.

So the lookup is retried, and inside a container the fallback is refused: the
process fails to start instead, Docker restarts it, and it keeps failing until
Postgres answers -- which is the same self-healing the compose dependency
would have given, but one that survives a reboot.
"""

import os
import re
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

SERVICE_HOST = "postgres"
# Ten tries a second apart. A compose network is usually ready in under a
# second; this covers a cold boot where Docker's DNS trails the containers it
# started. Longer than this is the supervisor's job, not a sleep's.
RESOLVE_ATTEMPTS = 10
RESOLVE_DELAY_SECONDS = 1.0


def in_container() -> bool:
    """Whether this process is running inside a container.

    Two signals because neither is guaranteed: Docker writes /.dockerenv, and
    container runtimes leave their name in the cgroup path. Asked only to
    decide whether the 127.0.0.1 fallback could possibly be correct, so a
    false negative costs the old behaviour and a false positive costs a
    refusal to start -- both loud, neither silent.
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/self/cgroup").read_text()
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def _host_fallback(url: str) -> str:
    """Point the URL at the port compose publishes on the host.

    The host and port are replaced together. The previous version replaced
    ``@postgres:5432`` and then, as a second pass, the bare ``@postgres:`` with
    a trailing slash appended -- which on any other port produced
    ``@127.0.0.1:5433/5432/intraday_sentinel``.
    """
    host_port = os.getenv("HOST_POSTGRES_PORT", "5433")
    return re.sub(rf"@{re.escape(SERVICE_HOST)}:\d+", f"@127.0.0.1:{host_port}", url, count=1)


def _resolved_db_url(url: str, *, sleep=time.sleep) -> str:  # noqa: ANN001 - injected for tests
    if f"@{SERVICE_HOST}:" not in url:
        return url
    for attempt in range(1, RESOLVE_ATTEMPTS + 1):
        try:
            socket.gethostbyname(SERVICE_HOST)
        except OSError:
            if attempt < RESOLVE_ATTEMPTS:
                sleep(RESOLVE_DELAY_SECONDS)
            continue
        return url
    if in_container():
        # Refusing is the point. Rewriting to 127.0.0.1 here would start a
        # process that can never reach a database and will not say so again
        # after the first error, which is how four days went by.
        raise RuntimeError(
            f"{SERVICE_HOST!r} did not resolve after {RESOLVE_ATTEMPTS} attempts and this process is in a "
            "container, where the 127.0.0.1 fallback cannot reach Postgres. Refusing to start with an "
            "address that cannot work; the supervisor should restart this until Postgres answers."
        )
    return _host_fallback(url)


settings = get_settings()
engine = create_async_engine(
    _resolved_db_url(str(settings.database_url)), pool_pre_ping=True, pool_size=10, max_overflow=10
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
