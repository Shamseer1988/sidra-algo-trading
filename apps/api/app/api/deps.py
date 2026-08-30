from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import UserRole
from app.db.session import get_db_session
from app.services.auth import decode_access_token

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def require_roles(*roles: UserRole) -> Callable:
    async def dependency(
        settings: AppSettings,
        access_token: Annotated[str | None, Cookie()] = None,
    ) -> tuple[str, UserRole]:
        if not access_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        try:
            user_id, role = decode_access_token(access_token, settings)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc
        if role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return str(user_id), role

    return dependency
