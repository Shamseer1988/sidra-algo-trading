from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import User, UserRole, UserSession
from app.db.session import get_db_session
from app.services.auth import decode_access_token

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def current_user(
    session: DbSession,
    settings: AppSettings,
    access_token: Annotated[str | None, Cookie()] = None,
) -> User:
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        user_id, _, session_id = decode_access_token(access_token, settings)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session no longer valid")
    active_session = await session.get(UserSession, session_id)
    if (
        active_session is None
        or active_session.user_id != user.id
        or active_session.revoked_at
        or active_session.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session no longer valid")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_roles(*roles: UserRole) -> Callable:
    async def dependency(
        user: CurrentUser,
    ) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return dependency
