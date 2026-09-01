"""Server-only Upstox OAuth exchange and encrypted credential storage."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.db.models import BrokerCredential
from app.db.session import SessionLocal

UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROVIDER = "UPSTOX"


class UpstoxOAuthError(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    if not settings.upstox_token_encryption_key:
        raise UpstoxOAuthError("UPSTOX_TOKEN_ENCRYPTION_KEY is required for OAuth renewal")
    try:
        return Fernet(settings.upstox_token_encryption_key.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise UpstoxOAuthError("UPSTOX_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def next_upstox_expiry(now: datetime | None = None) -> datetime:
    ist = ZoneInfo("Asia/Kolkata")
    current = (now or datetime.now(UTC)).astimezone(ist)
    expiry = datetime.combine(current.date(), time(3, 30), tzinfo=ist)
    if current >= expiry:
        expiry += timedelta(days=1)
    return expiry.astimezone(UTC)


async def exchange_authorization_code(settings: Settings, code: str) -> str:
    if not settings.upstox_oauth_is_configured:
        raise UpstoxOAuthError("Upstox OAuth settings are incomplete")
    payload = {
        "code": code,
        "client_id": settings.upstox_api_key,
        "client_secret": settings.upstox_api_secret,
        "redirect_uri": str(settings.upstox_redirect_uri),
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(UPSTOX_TOKEN_URL, data=payload, headers={"Accept": "application/json"})
        response.raise_for_status()
        token = response.json().get("access_token")
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstoxOAuthError("Upstox token exchange failed") from exc
    if not isinstance(token, str) or not token:
        raise UpstoxOAuthError("Upstox token exchange returned no access token")
    return token


async def store_access_token(settings: Settings, token: str, updated_by_user_id) -> datetime:
    expires_at = next_upstox_expiry()
    encrypted = _fernet(settings).encrypt(token.encode("utf-8")).decode("utf-8")
    async with SessionLocal() as session:
        stored = await session.get(BrokerCredential, PROVIDER)
        if stored is None:
            session.add(
                BrokerCredential(
                    provider=PROVIDER,
                    encrypted_access_token=encrypted,
                    expires_at=expires_at,
                    updated_by_user_id=updated_by_user_id,
                )
            )
        else:
            stored.encrypted_access_token = encrypted
            stored.expires_at = expires_at
            stored.updated_by_user_id = updated_by_user_id
        await session.commit()
    return expires_at


async def load_access_token(settings: Settings) -> str | None:
    if settings.upstox_token_encryption_key:
        async with SessionLocal() as session:
            stored = await session.get(BrokerCredential, PROVIDER)
        if stored and stored.expires_at > datetime.now(UTC):
            try:
                return _fernet(settings).decrypt(stored.encrypted_access_token.encode("utf-8")).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                raise UpstoxOAuthError("Stored Upstox credential cannot be decrypted") from None
    return settings.upstox_access_token
