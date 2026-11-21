"""U6: the first administrator from the sign-in page, and Settings > Users.

The claims that matter: the unauthenticated first-admin door is shut once any
human account exists; the automation account is invisible and untouchable;
nobody can lock themselves out or remove the last administrator; and switching
an account off or resetting its password ends its sessions.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import delete, select

from app.api.routes import users as routes
from app.core.config import get_settings
from app.db.models import AuditLog, User, UserRole, UserSession
from app.db.session import SessionLocal
from app.services import live_session_open, user_admin
from app.services.auth import verify_password

PASSWORD = "correct horse battery"


def _email() -> str:
    return f"u6-{uuid4().hex[:10]}@test.invalid"


@pytest.fixture
async def made():
    """Accounts made by a test, removed afterwards."""
    emails: list[str] = []
    yield emails
    async with SessionLocal() as session:
        ids = (await session.scalars(select(User.id).where(User.email.in_(emails)))).all()
        if ids:
            await session.execute(delete(AuditLog).where(AuditLog.user_id.in_(ids)))
            await session.execute(delete(User).where(User.id.in_(ids)))
        await session.commit()


async def _user(made: list[str], role: UserRole = UserRole.ADMIN) -> User:
    async with SessionLocal() as session:
        email = _email()
        made.append(email)
        user = await user_admin.create_user(session, email, PASSWORD, role)
        await session.commit()
        await session.refresh(user)
        return user


def test_the_automation_address_is_the_one_the_session_opener_uses():
    assert user_admin.AUTOMATION_EMAIL == live_session_open.AUTOMATION_EMAIL


@pytest.mark.parametrize("password", ["short", "elevenchars", " leading space ok", "trailing space ok "])
def test_weak_or_padded_passwords_are_refused(password):
    with pytest.raises(user_admin.UserAdminError):
        user_admin.check_password(password)


@pytest.mark.parametrize(
    "email", ["", "no-at-sign", "@x.com", "x@", "automation@sidra.local", " AUTOMATION@sidra.local "]
)
def test_bad_or_reserved_emails_are_refused(email):
    with pytest.raises(user_admin.UserAdminError):
        user_admin.normalise_email(email)


async def test_first_admin_is_refused_once_a_human_account_exists(made):
    await _user(made)
    async with SessionLocal() as session:
        assert not await user_admin.needs_first_admin(session)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.first_admin(routes.FirstAdmin(email=_email(), password=PASSWORD), session)
    assert refused.value.status_code == 409


async def test_the_automation_account_does_not_count_and_is_not_listed(made):
    async with SessionLocal() as session:
        listed = await user_admin.list_users(session)
        assert all(u.email != user_admin.AUTOMATION_EMAIL for u in listed)
        robot = await session.scalar(select(User).where(User.email == user_admin.AUTOMATION_EMAIL))
        if robot is not None:
            with pytest.raises(user_admin.UserAdminError):
                await user_admin.reset_password(session, robot.id, PASSWORD)


async def test_status_hands_out_a_csrf_cookie_only_while_setup_is_open(monkeypatch):
    async def yes(_session):
        return True

    monkeypatch.setattr(user_admin, "needs_first_admin", yes)
    response = Response()
    async with SessionLocal() as session:
        assert (await routes.setup_status(response, session, get_settings())).needs_first_admin
    assert "csrf_token=" in response.headers.get("set-cookie", "")

    async def no(_session):
        return False

    monkeypatch.setattr(user_admin, "needs_first_admin", no)
    response = Response()
    async with SessionLocal() as session:
        assert not (await routes.setup_status(response, session, get_settings())).needs_first_admin
    assert "set-cookie" not in response.headers


async def test_an_admin_adds_a_viewer_and_a_duplicate_is_refused(made):
    actor = await _user(made)
    email = _email()
    made.append(email)
    async with SessionLocal() as session:
        view = await routes.add_user(
            routes.NewUser(email=email.upper(), password=PASSWORD, role="VIEWER"), session, actor
        )
    assert view.email == email and view.role == "VIEWER" and view.is_active
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.add_user(routes.NewUser(email=email, password=PASSWORD), session, actor)
    assert refused.value.status_code == 422


async def test_nobody_can_demote_or_switch_off_themselves(made):
    actor = await _user(made)
    for change in ({"role": UserRole.VIEWER}, {"is_active": False}):
        async with SessionLocal() as session:
            with pytest.raises(user_admin.UserAdminError, match="your own"):
                await user_admin.change_user(
                    session, actor, actor.id, role=change.get("role"), is_active=change.get("is_active")
                )


async def test_the_last_active_administrator_cannot_be_removed(made, monkeypatch):
    actor = await _user(made)
    other = await _user(made)

    async def only_one(_session):
        return 1

    monkeypatch.setattr(user_admin, "_active_admins", only_one)
    async with SessionLocal() as session:
        with pytest.raises(user_admin.UserAdminError, match="last active administrator"):
            await user_admin.change_user(session, actor, other.id, role=UserRole.TRADER, is_active=None)
        with pytest.raises(user_admin.UserAdminError, match="last active administrator"):
            await user_admin.change_user(session, actor, other.id, role=None, is_active=False)


async def test_switching_an_account_off_ends_its_sessions(made):
    actor = await _user(made)
    target = await _user(made, UserRole.TRADER)
    async with SessionLocal() as session:
        session.add(
            UserSession(
                user_id=target.id,
                refresh_token_hash=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.commit()
    async with SessionLocal() as session:
        view = await routes.change_user(target.id, routes.UserChange(is_active=False), session, actor)
    assert not view.is_active
    async with SessionLocal() as session:
        open_sessions = (
            await session.scalars(
                select(UserSession).where(UserSession.user_id == target.id, UserSession.revoked_at.is_(None))
            )
        ).all()
    assert open_sessions == []


async def test_a_reset_password_works_and_the_old_one_does_not(made):
    actor = await _user(made)
    target = await _user(made, UserRole.VIEWER)
    async with SessionLocal() as session:
        await routes.reset_password(target.id, routes.PasswordReset(password="a brand new passphrase"), session, actor)
    async with SessionLocal() as session:
        stored = await session.get(User, target.id)
    assert verify_password("a brand new passphrase", stored.password_hash)
    assert not verify_password(PASSWORD, stored.password_hash)


async def test_changing_your_own_password_needs_the_current_one(made):
    me = await _user(made, UserRole.TRADER)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as refused:
            await routes.change_own_password(
                routes.OwnPassword(current_password="not my password", new_password="another long passphrase"),
                session,
                SimpleNamespace(id=me.id),
            )
    assert refused.value.status_code == 422
    async with SessionLocal() as session:
        await routes.change_own_password(
            routes.OwnPassword(current_password=PASSWORD, new_password="another long passphrase"),
            session,
            SimpleNamespace(id=me.id),
        )
    async with SessionLocal() as session:
        assert verify_password("another long passphrase", (await session.get(User, me.id)).password_hash)


async def test_the_checklist_points_every_step_at_a_screen(made):
    actor = await _user(made)
    async with SessionLocal() as session:
        items = await routes.checklist(session, get_settings(), actor)
    ids = [item.id for item in items]
    assert ids[0] == "admin" and items[0].done
    assert {"upstox_app", "upstox_login", "telegram", "trading_controls", "charges", "strategies"} <= set(ids)
    assert all(item.workspace for item in items)
