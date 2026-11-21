"""Strategies > Subscriptions: the intraday watchlist and benchmark from the screen.

The claims that matter: with nothing saved every reader sees ``.env``; a saved
list replaces both the subscriptions and the benchmark; a search shows NSE's
official company name so a share cannot be added by mistake; only known NSE
equities and listed benchmarks can be saved; and a save needs a confirmation
and a reason and is audited with what was added and removed.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.routes import watchlist as routes
from app.core.config import get_settings
from app.db.models import ApplicationSetting, AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services import user_admin, watchlist_store
from app.services.watchlist_store import Listing, SavedWatchlist

ADANIPORTS = "NSE_EQ|INE742F01042"
OUTSIDE = "NSE_EQ|INE000TEST01"


@pytest.fixture(autouse=True)
def small_master(monkeypatch):
    """Upstox's list stood in for by two entries; no download in tests."""

    async def master(force=False):
        return (
            {
                OUTSIDE: Listing(OUTSIDE, "TESTCO", "TEST COMPANY LIMITED", False, "Upstox NSE list"),
                ADANIPORTS: Listing(ADANIPORTS, "ADANIPORTS", "ADANI PORT & SEZ LTD", False, "Upstox NSE list"),
            },
            {"NSE_INDEX|Nifty 50": "Nifty 50", "NSE_INDEX|Nifty Bank": "Nifty Bank"},
        )

    monkeypatch.setattr(watchlist_store, "_upstox_master", master)


@pytest.fixture
async def admin():
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, watchlist_store.WATCHLIST_KEY)
        saved = row.value if row is not None else None
        user = await user_admin.create_user(
            session, f"wl-{uuid4().hex[:10]}@test.invalid", "correct horse battery", UserRole.ADMIN
        )
        await session.commit()
        await session.refresh(user)
    yield user
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, watchlist_store.WATCHLIST_KEY)
        if saved is None and row is not None:
            await session.delete(row)
        elif saved is not None:
            if row is None:
                session.add(ApplicationSetting(key=watchlist_store.WATCHLIST_KEY, value=saved))
            else:
                row.value, row.updated_by_user_id = saved, None
        await session.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
        await session.flush()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


def test_nothing_saved_means_the_environment_applies():
    settings = get_settings()
    assert watchlist_store.apply(settings, None) is settings


def test_a_saved_list_replaces_the_subscriptions_and_the_benchmark():
    settings = get_settings().model_copy(update={"upstox_subscriptions": "NSE_EQ|A,NSE_EQ|B"})
    applied = watchlist_store.apply(
        settings, SavedWatchlist(instruments=[ADANIPORTS, OUTSIDE], benchmark="NSE_INDEX|Nifty Bank")
    )
    assert applied.upstox_subscriptions == f"{ADANIPORTS},{OUTSIDE}"
    assert applied.upstox_nifty_benchmark_key == "NSE_INDEX|Nifty Bank"
    assert settings.upstox_subscriptions == "NSE_EQ|A,NSE_EQ|B"


async def test_a_symbol_search_shows_the_official_company_name():
    (first, *_) = await watchlist_store.search("adaniports")
    assert first.symbol == "ADANIPORTS"
    assert first.name == "Adani Ports and Special Economic Zone Ltd."
    assert first.in_nifty200 and first.instrument_key == ADANIPORTS


async def test_a_name_search_finds_the_share_and_nifty200_comes_first():
    results = await watchlist_store.search("adani ports")
    assert results[0].symbol == "ADANIPORTS"
    outside = await watchlist_store.search("TESTCO")
    assert outside[0].instrument_key == OUTSIDE and not outside[0].in_nifty200
    assert await watchlist_store.search("a") == []


@pytest.mark.parametrize(
    ("instruments", "benchmark", "message"),
    [
        ([], "NSE_INDEX|Nifty 50", "at least one"),
        (["NSE_EQ|NOT-A-REAL-KEY"], "NSE_INDEX|Nifty 50", "Not an NSE equity"),
        ([ADANIPORTS], "NSE_INDEX|Made Up", "benchmark"),
        ([ADANIPORTS], "NSE_INDEX|Nifty 500", "does not list"),
    ],
)
async def test_unknown_shares_and_benchmarks_are_refused(instruments, benchmark, message):
    with pytest.raises(watchlist_store.WatchlistError, match=message):
        await watchlist_store.validate(instruments, benchmark)


async def test_duplicates_are_dropped_and_order_is_kept():
    saved = await watchlist_store.validate([OUTSIDE, ADANIPORTS, OUTSIDE], "NSE_INDEX|Nifty 50")
    assert saved.instruments == [OUTSIDE, ADANIPORTS]


async def test_a_save_needs_confirmation(admin):
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.save(
                routes.SaveRequest(instruments=[ADANIPORTS], benchmark="NSE_INDEX|Nifty 50", reason="x"),
                session,
                get_settings(),
                admin,
            )
    assert refused.value.status_code == 422


async def test_a_save_is_stored_audited_and_shown_and_reset_returns_to_env(admin):
    settings = get_settings().model_copy(update={"upstox_subscriptions": OUTSIDE})
    async with SessionLocal() as session:
        view = await routes.save(
            routes.SaveRequest(
                instruments=[OUTSIDE, ADANIPORTS], benchmark="NSE_INDEX|Nifty 50", reason="add ports", confirm=True
            ),
            session,
            settings,
            admin,
        )
    assert view.source == "screen"
    assert [share.symbol for share in view.shares] == ["TESTCO", "ADANIPORTS"]
    assert view.shares[1].name == "Adani Ports and Special Economic Zone Ltd."
    async with SessionLocal() as session:
        entry = await session.scalar(
            select(AuditLog).where(AuditLog.user_id == admin.id, AuditLog.event_type == "watchlist.updated")
        )
    assert entry.metadata_json["added"] == [ADANIPORTS] and entry.metadata_json["removed"] == []
    assert entry.metadata_json["reason"] == "add ports"
    async with SessionLocal() as session:
        reset = await routes.use_server_file(session, settings, admin)
    assert reset.source == "server file"
    assert [share.instrument_key for share in reset.shares] == [OUTSIDE]
