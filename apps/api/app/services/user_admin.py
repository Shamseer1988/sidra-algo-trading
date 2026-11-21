"""Users and roles, managed from the screen; and the very first administrator.

Before this, an account could only be made with ``python -m app.cli
create-admin`` inside the container, which is exactly the step a person
running the app without a developer cannot do. Now:

* a fresh deployment with no human account offers "Create the first
  administrator" on the sign-in page -- once. The moment any human account
  exists that door is shut, whatever the request says;
* an administrator adds people from Settings > Users with a role (Admin,
  Trader, Viewer), resets a forgotten password, and switches an account off.

The guards are the point. The system's own automation account
(``automation@sidra.local``, inactive, used by the 8:45 AM session opener) is
never listed, counted or editable here. Nobody can switch off or demote
themselves, and the last active administrator cannot be removed, so the app
can never be left with nobody able to change a setting. Switching an account
off or resetting its password ends every session it has, at once.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserRole, UserSession
from app.services.auth import hash_password

# The same address as ``live_session_open.AUTOMATION_EMAIL`` (a test holds them
# equal); spelled out so managing people does not import the live-session code.
AUTOMATION_EMAIL = "automation@sidra.local"
MIN_PASSWORD = 12


class UserAdminError(ValueError):
    pass


def human(query):  # noqa: ANN001, ANN201
    """Every query here skips the automation account."""
    return query.where(User.email != AUTOMATION_EMAIL)


def check_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise UserAdminError(f"A password needs at least {MIN_PASSWORD} characters.")
    if len(password) > 256:
        raise UserAdminError("That password is too long.")
    if password.strip() != password or not password.strip():
        raise UserAdminError("A password cannot start or end with a space.")
    return password


def normalise_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@") or len(value) > 320:
        raise UserAdminError("That does not look like an email address.")
    if value == AUTOMATION_EMAIL:
        raise UserAdminError("That address belongs to the system and cannot be used.")
    return value


async def human_count(session: AsyncSession) -> int:
    return int(await session.scalar(human(select(func.count()).select_from(User))) or 0)


async def needs_first_admin(session: AsyncSession) -> bool:
    """True only on a deployment with no human account at all."""
    return await human_count(session) == 0


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.scalars(human(select(User)).order_by(User.created_at))).all())


async def _active_admins(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            human(select(func.count()).select_from(User)).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        or 0
    )


async def end_sessions(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def create_user(session: AsyncSession, email: str, password: str, role: UserRole) -> User:
    address = normalise_email(email)
    check_password(password)
    if await session.scalar(select(User).where(User.email == address)):
        raise UserAdminError("There is already an account with that email.")
    user = User(email=address, password_hash=hash_password(password), role=role, is_active=True)
    session.add(user)
    await session.flush()
    return user


async def create_first_admin(session: AsyncSession, email: str, password: str) -> User:
    """The one unauthenticated write in the app, and only while no human account exists.

    The count and the insert happen under a transaction-scoped advisory lock,
    so two browsers racing on a fresh install cannot both become the first
    administrator.
    """
    await session.execute(select(func.pg_advisory_xact_lock(0x5ED1A_0001)))
    if not await needs_first_admin(session):
        raise UserAdminError("This app already has an administrator. Sign in instead.")
    return await create_user(session, email, password, UserRole.ADMIN)


async def _editable(session: AsyncSession, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or user.email == AUTOMATION_EMAIL:
        raise UserAdminError("No such account.")
    return user


async def change_user(
    session: AsyncSession, actor: User, user_id: UUID, *, role: UserRole | None, is_active: bool | None
) -> tuple[User, list[str]]:
    """Change an account's role or switch it on/off. Returns what changed."""
    user = await _editable(session, user_id)
    changed: list[str] = []
    if user.id == actor.id and ((role is not None and role != user.role) or (is_active is not None and not is_active)):
        raise UserAdminError("You cannot change your own role or switch yourself off. Ask another administrator.")
    losing_admin = (
        user.role == UserRole.ADMIN
        and user.is_active
        and ((role is not None and role != UserRole.ADMIN) or (is_active is False))
    )
    if losing_admin and await _active_admins(session) <= 1:
        raise UserAdminError("This is the last active administrator. Make someone else an administrator first.")
    if role is not None and role != user.role:
        user.role = role
        changed.append("role")
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        changed.append("is_active")
        if not is_active:
            await end_sessions(session, user.id)
        else:
            user.failed_login_count = 0
            user.locked_until = None
    return user, changed


async def reset_password(session: AsyncSession, user_id: UUID, password: str) -> User:
    """An administrator sets a new password for someone; their sessions end."""
    user = await _editable(session, user_id)
    user.password_hash = hash_password(check_password(password))
    user.failed_login_count = 0
    user.locked_until = None
    await end_sessions(session, user.id)
    return user
