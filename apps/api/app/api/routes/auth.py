from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import AppSettings, DbSession
from app.db.models import AuditLog, LoginHistory, User
from app.services.auth import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class LoginResponse(BaseModel):
    email: EmailStr
    role: str


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response, session: DbSession, settings: AppSettings) -> LoginResponse:
    email = str(payload.email).lower()
    user = await session.scalar(select(User).where(User.email == email))
    ip_address = request.client.host if request.client else None
    authenticated = bool(user and user.is_active and verify_password(payload.password, user.password_hash))
    session.add(LoginHistory(user_id=user.id if authenticated else None, email_attempted=email, success=authenticated, ip_address=ip_address))
    if not authenticated or user is None:
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    user.failed_login_count = 0
    session.add(AuditLog(user_id=user.id, event_type="auth.login", ip_address=ip_address, metadata_json={}))
    await session.commit()
    response.set_cookie(
        key="access_token",
        value=create_access_token(user, settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_minutes * 60,
        path="/",
    )
    return LoginResponse(email=user.email, role=user.role.value)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie("access_token", path="/")
