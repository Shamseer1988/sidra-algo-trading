"""Settings > Connections: broker and Telegram credentials, entered and checked from the screen.

Nothing secret is ever returned: each field says whether it is set, whether it
came from this screen or the server's ``.env``, and for identifiers the last
characters. Saves record the names of the fields that changed, never values.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AppSettings, CurrentUser, require_roles
from app.db.models import AuditLog, BrokerCredential, User, UserRole
from app.db.session import SessionLocal
from app.services import credential_store
from app.services.telegram_config import load_telegram_config, save_telegram_config
from app.services.upstox_oauth import PROVIDER as UPSTOX_TOKEN_PROVIDER
from app.services.upstox_oauth import UpstoxOAuthError, load_access_token

router = APIRouter(prefix="/connections", tags=["Connections"])

UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


class FieldView(BaseModel):
    name: str
    label: str
    kind: str
    help: str
    configured: bool
    source: str | None
    shown: str | None
    value: bool | None


class UpstoxView(BaseModel):
    fields: list[FieldView]
    login_ready: bool
    auto_login_ready: bool
    token_valid_until: datetime | None


class TelegramView(BaseModel):
    fields: list[FieldView]
    ready: bool


class ConnectionsView(BaseModel):
    encryption_ready: bool
    upstox: UpstoxView
    telegram: TelegramView


class UpstoxSave(BaseModel):
    values: dict[str, str | bool] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=300)


class TelegramSave(BaseModel):
    bot_token: str = Field(default="", max_length=256)
    chat_id: str = Field(default="", max_length=64)
    reason: str | None = Field(default=None, max_length=300)


class CheckResult(BaseModel):
    ok: bool
    detail: str
    checked_at: datetime


def _telegram_fields(settings, saved: dict[str, str]) -> list[FieldView]:  # noqa: ANN001
    rows = []
    for name, label, kind, help_text in (
        ("bot_token", "Bot token", "secret", "From @BotFather after /newbot. Never shown again once saved."),
        (
            "chat_id",
            "Chat ID",
            "identifier",
            "Where alerts go: your own chat with the bot, or a group. Send the bot a message first.",
        ),
    ):
        env_value = getattr(settings, f"telegram_{name}")
        if name in saved:
            source, value = "screen", saved[name]
        elif env_value:
            source, value = "server file", env_value
        else:
            source, value = None, None
        spec = credential_store.FieldSpec(name, f"telegram_{name}", label, kind, help_text)
        rows.append(
            FieldView(
                name=name,
                label=label,
                kind=kind,
                help=help_text,
                configured=bool(value),
                source=source,
                shown=credential_store.masked(spec, value),
                value=None,
            )
        )
    return rows


async def _view(settings) -> ConnectionsView:  # noqa: ANN001
    saved = await credential_store.load_saved(settings, credential_store.UPSTOX_APP)
    effective = credential_store.apply(settings, credential_store.UPSTOX_APP, saved)
    async with SessionLocal() as session:
        token_row = await session.get(BrokerCredential, UPSTOX_TOKEN_PROVIDER)
    valid_until = token_row.expires_at if token_row and token_row.expires_at > datetime.now(UTC) else None
    telegram_saved = await load_telegram_config(settings)
    telegram_fields = _telegram_fields(settings, telegram_saved)
    return ConnectionsView(
        encryption_ready=credential_store.server_fernet(settings) is not None,
        upstox=UpstoxView(
            fields=[
                FieldView(**row) for row in credential_store.describe(settings, credential_store.UPSTOX_APP, saved)
            ],
            login_ready=effective.upstox_oauth_is_configured,
            auto_login_ready=effective.upstox_auto_auth_is_configured,
            token_valid_until=valid_until,
        ),
        telegram=TelegramView(fields=telegram_fields, ready=all(field.configured for field in telegram_fields)),
    )


async def _audit(user: User, event_type: str, metadata: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        session.add(AuditLog(user_id=user.id, event_type=event_type, metadata_json=metadata))
        await session.commit()


@router.get("", response_model=ConnectionsView)
async def connections(settings: AppSettings, _: CurrentUser) -> ConnectionsView:
    return await _view(settings)


@router.put("/upstox", response_model=ConnectionsView)
async def save_upstox(
    request: UpstoxSave, settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ConnectionsView:
    try:
        changed = await credential_store.save(
            settings, credential_store.UPSTOX_APP, request.values, request.clear, user.id
        )
    except credential_store.CredentialError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await _audit(user, "connections.upstox_updated", {"changed": changed, "reason": request.reason})
    return await _view(settings)


@router.put("/telegram", response_model=ConnectionsView)
async def save_telegram(
    request: TelegramSave, settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ConnectionsView:
    """Save the bot token and chat id; a blank box keeps what is there (saved, else ``.env``)."""
    saved = await load_telegram_config(settings)
    bot_token = request.bot_token.strip() or saved.get("bot_token") or settings.telegram_bot_token or ""
    chat_id = request.chat_id.strip() or saved.get("chat_id") or settings.telegram_chat_id or ""
    if not bot_token or not chat_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Both the bot token and the chat ID are needed.")
    if ":" not in bot_token:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "That does not look like a bot token (123456:ABC...)."
        )
    try:
        await save_telegram_config(settings, bot_token, chat_id, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    changed = [
        name for name, value in (("bot_token", request.bot_token), ("chat_id", request.chat_id)) if value.strip()
    ]
    await _audit(user, "connections.telegram_updated", {"changed": changed, "reason": request.reason})
    return await _view(settings)


@router.post("/upstox/check", response_model=CheckResult)
async def check_upstox(settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))) -> CheckResult:
    """Ask Upstox who the current token belongs to. Read-only: places and changes nothing."""
    now = datetime.now(UTC)
    try:
        token = await load_access_token(settings)
    except UpstoxOAuthError as exc:
        return CheckResult(ok=False, detail=str(exc), checked_at=now)
    if not token:
        return CheckResult(
            ok=False,
            detail="No valid access token. Use 'Log in to Upstox' or the morning auto-login.",
            checked_at=now,
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                UPSTOX_PROFILE_URL, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        return CheckResult(ok=False, detail=f"Could not reach Upstox: {type(exc).__name__}", checked_at=now)
    if response.status_code == 401:
        return CheckResult(
            ok=False, detail="Upstox refused the token (expired or revoked). Log in again.", checked_at=now
        )
    if response.status_code != 200:
        return CheckResult(ok=False, detail=f"Upstox answered {response.status_code}.", checked_at=now)
    try:
        data = response.json().get("data", {}) or {}
    except ValueError:
        data = {}
    user_id = str(data.get("user_id") or "")
    exchanges = ", ".join(data.get("exchanges") or []) or "unknown"
    await _audit(user, "connections.upstox_checked", {"ok": True})
    return CheckResult(
        ok=True,
        detail=f"Connected to Upstox account ••••{user_id[-3:] if user_id else '?'} (exchanges: {exchanges}).",
        checked_at=now,
    )
