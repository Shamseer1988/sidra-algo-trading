from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from redis.asyncio import Redis
from sqlalchemy import desc, select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, LoginHistory, User, UserRole, UserSession
from app.services.auth import (
    create_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class LoginResponse(BaseModel):
    email: EmailStr
    role: str


class CurrentUserResponse(BaseModel):
    email: EmailStr
    role: str
    is_active: bool


class SessionResponse(BaseModel):
    id: UUID
    created_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None


class AuditLogResponse(BaseModel):
    id: UUID
    event_type: str
    created_at: datetime
    user_id: UUID | None
    ip_address: str | None
    message: str | None
    metadata_json: dict


def _set_session_cookies(
    response: Response,
    user: User,
    user_session: UserSession,
    settings: AppSettings,
    refresh_token: str,
    csrf_token: str,
) -> None:
    response.set_cookie(
        "access_token",
        create_access_token(user, user_session.id, settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_minutes * 60,
        path="/",
    )
    response.set_cookie(
        "refresh_token",
        refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1/auth",
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    response.delete_cookie("csrf_token", path="/")


async def _consume_login_attempt(settings: AppSettings, email: str, ip_address: str | None) -> None:
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    key = f"auth:login:{ip_address or 'unknown'}:{email}"
    try:
        attempts = await redis.incr(key)
        if attempts == 1:
            await redis.expire(key, settings.login_rate_limit_minutes * 60)
        if attempts > settings.login_rate_limit_attempts:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts; try again later"
            )
    except HTTPException:
        raise
    except Exception:
        # Login remains available during a Redis outage, but account lockout still applies.
        return
    finally:
        await redis.aclose()


async def _clear_login_attempts(settings: AppSettings, email: str, ip_address: str | None) -> None:
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        await redis.delete(f"auth:login:{ip_address or 'unknown'}:{email}")
    except Exception:
        pass
    finally:
        await redis.aclose()


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, request: Request, response: Response, session: DbSession, settings: AppSettings
) -> LoginResponse:
    email = str(payload.email).lower()
    user = await session.scalar(select(User).where(User.email == email))
    ip_address = request.client.host if request.client else None
    await _consume_login_attempt(settings, email, ip_address)
    is_locked = bool(user and user.locked_until and user.locked_until > datetime.now(UTC))
    authenticated = bool(
        user and user.is_active and not is_locked and verify_password(payload.password, user.password_hash)
    )
    session.add(
        LoginHistory(
            user_id=user.id if authenticated else None,
            email_attempted=email,
            success=authenticated,
            ip_address=ip_address,
        )
    )
    if not authenticated or user is None:
        if user and user.is_active and not is_locked:
            user.failed_login_count += 1
            if user.failed_login_count >= settings.max_failed_login_attempts:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=settings.login_lockout_minutes)
                session.add(
                    AuditLog(user_id=user.id, event_type="auth.account_locked", ip_address=ip_address, metadata_json={})
                )
            else:
                session.add(
                    AuditLog(user_id=user.id, event_type="auth.login_failed", ip_address=ip_address, metadata_json={})
                )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    user.failed_login_count = 0
    refresh_token = generate_refresh_token()
    csrf_token = generate_csrf_token()
    user_session = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        ip_address=ip_address,
        user_agent=request.headers.get("user-agent", "")[:512] or None,
    )
    session.add(user_session)
    session.add(AuditLog(user_id=user.id, event_type="auth.login", ip_address=ip_address, metadata_json={}))
    await session.commit()
    await session.refresh(user_session)
    await _clear_login_attempts(settings, email, ip_address)
    _set_session_cookies(response, user, user_session, settings, refresh_token, csrf_token)
    return LoginResponse(email=user.email, role=user.role.value)


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse(email=user.email, role=user.role.value, is_active=user.is_active)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(session: DbSession, user: CurrentUser) -> list[SessionResponse]:
    rows = (
        await session.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > datetime.now(UTC),
            )
            .order_by(desc(UserSession.created_at))
        )
    ).all()
    return [
        SessionResponse(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: UUID, session: DbSession, user: CurrentUser) -> None:
    stored = await session.get(UserSession, session_id)
    if stored is None or stored.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        session.add(
            AuditLog(user_id=user.id, event_type="auth.session_revoked", metadata_json={"session_id": str(session_id)})
        )
        await session.commit()


@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    session: DbSession, _: User = Depends(require_roles(UserRole.ADMIN)), limit: int = 100
) -> list[AuditLogResponse]:
    safe_limit = min(max(limit, 1), 250)
    rows = (await session.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(safe_limit))).all()
    return [
        AuditLogResponse(
            id=row.id,
            event_type=row.event_type,
            created_at=row.created_at,
            user_id=row.user_id,
            ip_address=row.ip_address,
            message=row.message,
            metadata_json=row.metadata_json,
        )
        for row in rows
    ]


@router.post("/refresh", response_model=LoginResponse)
async def refresh_session(
    request: Request, response: Response, session: DbSession, settings: AppSettings
) -> LoginResponse:
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh session missing")
    stored = await session.scalar(
        select(UserSession).where(UserSession.refresh_token_hash == hash_refresh_token(token))
    )
    if stored is None or stored.revoked_at or stored.expires_at <= datetime.now(UTC):
        _clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh session invalid")
    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        _clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session no longer valid")
    stored.revoked_at = datetime.now(UTC)
    replacement_token = generate_refresh_token()
    replacement = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(replacement_token),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:512] or None,
    )
    session.add(replacement)
    session.add(AuditLog(user_id=user.id, event_type="auth.session_refreshed", metadata_json={}))
    await session.commit()
    await session.refresh(replacement)
    _set_session_cookies(response, user, replacement, settings, replacement_token, generate_csrf_token())
    return LoginResponse(email=user.email, role=user.role.value)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: DbSession, user: CurrentUser) -> None:
    token = request.cookies.get("refresh_token")
    if token:
        stored = await session.scalar(
            select(UserSession).where(UserSession.refresh_token_hash == hash_refresh_token(token))
        )
        if stored and stored.user_id == user.id and not stored.revoked_at:
            stored.revoked_at = datetime.now(UTC)
    session.add(AuditLog(user_id=user.id, event_type="auth.logout", metadata_json={}))
    await session.commit()
    _clear_session_cookies(response)
