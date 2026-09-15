"""Shared test fixtures.

The async engine in ``app.db.session`` is a module-level singleton, and its
connection pool holds connections bound to the event loop that opened them.
pytest-asyncio gives each test its own loop, so the second test in a module to
touch the database inherits pooled connections belonging to a loop that has
since closed, and asyncpg raises ``RuntimeError: Event loop is closed`` from
inside the teardown of an otherwise passing test.

Disposing the pool after every test costs a reconnect and removes a whole class
of failure that depends on test ordering, which is the worst kind to debug.
"""

import pytest

from app.db.session import engine


@pytest.fixture(autouse=True)
async def dispose_database_pool():
    yield
    await engine.dispose()
