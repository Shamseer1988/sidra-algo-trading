"""First-run setup and Settings > Users. See ``app.services.user_admin``.

``/setup/status`` and ``/setup/first-admin`` are the only routes a signed-out
browser can use besides login, and ``first-admin`` refuses as soon as any human
account exists. ``/setup/status`` also hands out the CSRF cookie the first-admin
form needs, so the CSRF check stays on for every write.
"""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.api.routes.settings import TRADING_KEY
from app.db.models import ApplicationSetting, AuditLog, User, UserRole
from app.services import user_admin
from app.services.auth import generate_csrf_token, verify_password
from app.services.charges import SWING_CHARGES_KEY
from app.services.paper_execution import PAPER_EXECUTION_KEY
from app.services.strategy_registry import STRATEGIES_KEY

router = APIRouter(tags=["Users & setup"])
Admin = Depends(require_roles(UserRole.ADMIN))
IST = ZoneInfo("Asia/Kolkata")


class SetupStatus(BaseModel):
    needs_first_admin: bool


class FirstAdmin(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class UserView(BaseModel):
    id: UUID
    email: str
    role: str
    is_active: bool
    locked: bool
    created_at: datetime


class NewUser(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    role: UserRole = UserRole.VIEWER


class UserChange(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str = Field(max_length=256)


class OwnPassword(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class ChecklistItem(BaseModel):
    id: str
    title: str
    done: bool
    detail: str
    workspace: str
    tab: str | None
    required: bool


def _view(user: User) -> UserView:
    return UserView(
        id=user.id,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        locked=bool(user.locked_until and user.locked_until > datetime.now(user.locked_until.tzinfo)),
        created_at=user.created_at,
    )


def _refuse(exc: user_admin.UserAdminError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# --- first run ------------------------------------------------------------


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status(response: Response, session: DbSession, settings: AppSettings) -> SetupStatus:
    needed = await user_admin.needs_first_admin(session)
    if needed:
        response.set_cookie(
            "csrf_token",
            generate_csrf_token(),
            httponly=False,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=3600,
            path="/",
        )
    return SetupStatus(needs_first_admin=needed)


@router.post("/setup/first-admin", response_model=UserView, status_code=status.HTTP_201_CREATED)
async def first_admin(request: FirstAdmin, session: DbSession) -> UserView:
    try:
        user = await user_admin.create_first_admin(session, request.email, request.password)
    except user_admin.UserAdminError as exc:
        await session.rollback()
        code = status.HTTP_409_CONFLICT if "already has" in str(exc) else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    session.add(AuditLog(user_id=user.id, event_type="setup.first_admin_created", metadata_json={"email": user.email}))
    await session.commit()
    return _view(user)


async def _setting(session, key: str):  # noqa: ANN001, ANN202
    row = await session.get(ApplicationSetting, key)
    return row.value if row is not None else None


@router.get("/setup/checklist", response_model=list[ChecklistItem])
async def checklist(session: DbSession, settings: AppSettings, _: CurrentUser) -> list[ChecklistItem]:
    """What a new deployment still needs, each step pointing at the screen that does it."""
    from app.api.routes.connections import _view as connections_view

    connections = await connections_view(settings)
    users = await user_admin.list_users(session)
    strategies = await _setting(session, STRATEGIES_KEY)
    enabled = [s for s in strategies or [] if isinstance(s, dict) and s.get("enabled")]
    live = [s for s in enabled if s.get("live_orders", True)]
    token = connections.upstox.token_valid_until
    return [
        ChecklistItem(
            id="admin",
            title="Administrator account",
            done=any(u.role == UserRole.ADMIN and u.is_active for u in users),
            detail=f"{len(users)} account(s). Add a Viewer for anyone who should only watch.",
            workspace="settings",
            tab="users",
            required=True,
        ),
        ChecklistItem(
            id="upstox_app",
            title="Upstox app keys",
            done=connections.upstox.login_ready,
            detail="API key, secret and redirect URL from the Upstox developer console.",
            workspace="settings",
            tab="connections",
            required=True,
        ),
        ChecklistItem(
            id="upstox_login",
            title="Logged in to Upstox today",
            done=token is not None,
            detail=(
                f"Token valid until {token.astimezone(IST).strftime('%d %b, %I:%M %p')} IST."
                if token
                else "Press 'Log in to Upstox', or let the 8:30 AM auto-login do it."
            ),
            workspace="settings",
            tab="connections",
            required=True,
        ),
        ChecklistItem(
            id="auto_login",
            title="Morning auto-login",
            done=connections.upstox.auto_login_ready,
            detail="Mobile, PIN and TOTP secret, so the app logs in by itself at 8:30 AM.",
            workspace="settings",
            tab="connections",
            required=False,
        ),
        ChecklistItem(
            id="telegram",
            title="Telegram alerts",
            done=connections.telegram.ready,
            detail="A bot token and chat ID, so the app can tell you what it did.",
            workspace="settings",
            tab="connections",
            required=False,
        ),
        ChecklistItem(
            id="trading_controls",
            title="Capital and risk limits",
            done=await _setting(session, TRADING_KEY) is not None,
            detail="Capital, risk per trade, trades a day and the daily loss stop, saved at least once.",
            workspace="settings",
            tab="intraday",
            required=True,
        ),
        ChecklistItem(
            id="charges",
            title="Charges checked",
            done=await _setting(session, PAPER_EXECUTION_KEY) is not None
            and await _setting(session, SWING_CHARGES_KEY) is not None,
            detail="Both rate cards saved after checking them against a contract note.",
            workspace="settings",
            tab="charges",
            required=False,
        ),
        ChecklistItem(
            id="strategies",
            title="A strategy switched on",
            done=bool(enabled),
            detail=(
                f"{len(enabled)} on, {len(live)} of them placing live orders."
                if enabled
                else "Set at least one strategy to Paper in Strategies."
            ),
            workspace="strategies",
            tab=None,
            required=True,
        ),
    ]


# --- users ----------------------------------------------------------------


async def _audit(session, actor: User, event: str, metadata: dict) -> None:  # noqa: ANN001
    session.add(AuditLog(user_id=actor.id, event_type=event, metadata_json=metadata))


@router.get("/users", response_model=list[UserView])
async def users(session: DbSession, _: User = Admin) -> list[UserView]:
    return [_view(user) for user in await user_admin.list_users(session)]


@router.post("/users", response_model=UserView, status_code=status.HTTP_201_CREATED)
async def add_user(request: NewUser, session: DbSession, actor: User = Admin) -> UserView:
    try:
        user = await user_admin.create_user(session, request.email, request.password, request.role)
    except user_admin.UserAdminError as exc:
        raise _refuse(exc) from exc
    await _audit(session, actor, "users.created", {"email": user.email, "role": user.role.value})
    await session.commit()
    return _view(user)


@router.put("/users/{user_id}", response_model=UserView)
async def change_user(user_id: UUID, request: UserChange, session: DbSession, actor: User = Admin) -> UserView:
    try:
        user, changed = await user_admin.change_user(
            session, actor, user_id, role=request.role, is_active=request.is_active
        )
    except user_admin.UserAdminError as exc:
        raise _refuse(exc) from exc
    if changed:
        await _audit(
            session,
            actor,
            "users.changed",
            {"email": user.email, "changed": changed, "role": user.role.value, "is_active": user.is_active},
        )
    await session.commit()
    return _view(user)


@router.post("/users/{user_id}/password", response_model=UserView)
async def reset_password(user_id: UUID, request: PasswordReset, session: DbSession, actor: User = Admin) -> UserView:
    try:
        user = await user_admin.reset_password(session, user_id, request.password)
    except user_admin.UserAdminError as exc:
        raise _refuse(exc) from exc
    await _audit(session, actor, "users.password_reset", {"email": user.email})
    await session.commit()
    return _view(user)


@router.post("/account/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_own_password(request: OwnPassword, session: DbSession, user: CurrentUser) -> None:
    """Change your own password. Every session ends, this one included; sign in again."""
    me = await session.scalar(select(User).where(User.id == user.id))
    if me is None or not verify_password(request.current_password, me.password_hash):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The current password is not right.")
    try:
        await user_admin.reset_password(session, me.id, request.new_password)
    except user_admin.UserAdminError as exc:
        raise _refuse(exc) from exc
    await _audit(session, me, "users.own_password_changed", {})
    await session.commit()
