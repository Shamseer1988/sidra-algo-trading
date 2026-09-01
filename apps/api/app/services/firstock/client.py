import hashlib
from dataclasses import dataclass
from typing import Any

import httpx
import pyotp

from app.core.config import Settings

FIRSTOCK_API_BASE_URL = "https://api.firstock.in/V1"


class FirstockError(RuntimeError):
    """Safe error type; messages must never include credentials or session tokens."""


@dataclass(frozen=True)
class FirstockSession:
    user_id: str
    session_token: str


class FirstockClient:
    """Thin, explicit wrapper for verified Firstock REST contracts only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _totp(self) -> str:
        if not self._settings.firstock_totp_secret:
            return ""
        return pyotp.TOTP(self._settings.firstock_totp_secret).now()

    async def login(self) -> FirstockSession:
        if not self._settings.firstock_is_configured:
            raise FirstockError("Firstock credentials are not configured")
        payload = {
            "userId": self._settings.firstock_user_id,
            "password": hashlib.sha256(self._settings.firstock_password.encode("utf-8")).hexdigest(),
            "TOTP": self._totp(),
            "vendorCode": self._settings.firstock_vendor_code,
            "apiKey": self._settings.firstock_api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.post(f"{FIRSTOCK_API_BASE_URL}/login", json=payload)
                response.raise_for_status()
                body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FirstockError("Firstock login request failed") from exc
        data = body.get("data") if body.get("status") == "success" else None
        token = data.get("susertoken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise FirstockError("Firstock rejected authentication")
        return FirstockSession(user_id=self._settings.firstock_user_id or "", session_token=token)
