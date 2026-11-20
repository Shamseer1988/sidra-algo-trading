"""Broker and Telegram credentials entered in Settings > Connections, encrypted at rest.

Until now every one of these lived only in the server's ``.env``. Now each can be
entered on the Connections screen. What is entered there is:

* **encrypted** with the server's key (``UPSTOX_TOKEN_ENCRYPTION_KEY``, the key
  that already protects the daily Upstox token) before it reaches the database;
* **never sent back**: the screen is told only whether a field is set, where it
  came from, and for identifiers the last few characters;
* **layered over ``.env``**: a field saved on the screen wins; a field never
  saved there keeps its ``.env`` value. With nothing saved, every reader sees
  exactly the settings it saw before this module existed.

The server key itself stays in ``.env``. It is the one thing that cannot live in
the database it protects.

Readers call ``effective_settings(settings)`` instead of using ``settings``
directly. Today those are the Upstox web login and its callback, the morning
auto-login and its startup check, and the Telegram sender. Order placement
reads only the daily access token (``upstox_oauth.load_access_token``), which
this module does not touch.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.db.models import BrokerCredential
from app.db.session import SessionLocal

logger = structlog.get_logger("credentials")

UPSTOX_APP = "UPSTOX_APP"


@dataclass(frozen=True)
class FieldSpec:
    name: str  # as the screen and the API name it
    setting: str  # the Settings attribute it overrides
    label: str
    kind: str  # "secret" (never shown), "identifier" (last characters shown), "text" (shown), "boolean"
    help: str


UPSTOX_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("api_key", "upstox_api_key", "API key", "identifier", "From Upstox Developer Apps > your app."),
    FieldSpec(
        "api_secret",
        "upstox_api_secret",
        "API secret",
        "secret",
        "From the same app page. Never shown again once saved.",
    ),
    FieldSpec(
        "redirect_uri",
        "upstox_redirect_uri",
        "Redirect URL",
        "text",
        "Must match the app's redirect URL in Upstox exactly, e.g. https://your-host/api/v1/market-data/upstox/callback.",
    ),
    FieldSpec(
        "mobile_number",
        "upstox_mobile_number",
        "Mobile number",
        "identifier",
        "The Upstox login mobile, for the morning auto-login.",
    ),
    FieldSpec("pin", "upstox_pin", "Login PIN", "secret", "The 6-digit Upstox PIN, for the morning auto-login."),
    FieldSpec(
        "totp_secret",
        "upstox_totp_secret",
        "TOTP secret",
        "secret",
        "The key behind your authenticator app's Upstox code (Upstox > Profile > 2FA setup).",
    ),
    FieldSpec(
        "auto_auth_enabled",
        "upstox_auto_auth_enabled",
        "Morning auto-login",
        "boolean",
        "Log in to Upstox by itself at 8:30 AM on trading days, so live trading has a fresh token.",
    ),
)
FIELDS = {UPSTOX_APP: UPSTOX_FIELDS}


class CredentialError(RuntimeError):
    pass


def server_fernet(settings: Settings) -> Fernet | None:
    """The server key, or None when ``.env`` has none (then nothing can be saved here)."""
    key = settings.upstox_token_encryption_key
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError):
        return None


async def load_saved(settings: Settings, provider: str) -> dict[str, Any]:
    """What the screen saved for ``provider``, decrypted; empty when nothing (or unreadable)."""
    fernet = server_fernet(settings)
    if fernet is None:
        return {}
    async with SessionLocal() as session:
        row = await session.get(BrokerCredential, provider)
    if row is None:
        return {}
    try:
        value = json.loads(fernet.decrypt(row.encrypted_access_token.encode("utf-8")))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        logger.warning("credentials.unreadable", provider=provider)
        return {}
    return value if isinstance(value, dict) else {}


def _clean(spec: FieldSpec, value: Any) -> Any:
    if spec.kind == "boolean":
        if not isinstance(value, bool):
            raise CredentialError(f"{spec.label} must be on or off.")
        return value
    if not isinstance(value, str):
        raise CredentialError(f"{spec.label} must be text.")
    value = value.strip()
    if len(value) > 512:
        raise CredentialError(f"{spec.label} is too long.")
    if spec.name == "redirect_uri" and value and not value.startswith(("https://", "http://")):
        raise CredentialError("Redirect URL must start with https://.")
    if spec.name == "pin" and value and not (value.isdigit() and 4 <= len(value) <= 8):
        raise CredentialError("Login PIN must be 4 to 8 digits.")
    if spec.name == "mobile_number" and value and not (value.isdigit() and len(value) == 10):
        raise CredentialError("Mobile number must be 10 digits.")
    return value


async def save(
    settings: Settings,
    provider: str,
    values: dict[str, Any],
    clear: list[str],
    user_id: Any,
) -> list[str]:
    """Merge ``values`` into what is saved, drop the fields in ``clear``, and store it encrypted.

    A blank text value means "leave as it is", so a form that shows secrets as
    empty boxes cannot wipe them by being saved. Removing a saved value is an
    explicit ``clear``, after which ``.env`` applies again. Returns the names of
    the fields that changed -- names only, never values.
    """
    fernet = server_fernet(settings)
    if fernet is None:
        raise CredentialError(
            "The server has no encryption key (UPSTOX_TOKEN_ENCRYPTION_KEY), so credentials cannot be stored safely. "
            "This is a one-time server setup step."
        )
    specs = {spec.name: spec for spec in FIELDS[provider]}
    unknown = sorted((set(values) | set(clear)) - set(specs))
    if unknown:
        raise CredentialError(f"Unknown field: {', '.join(unknown)}")
    saved = await load_saved(settings, provider)
    changed: list[str] = []
    for name, raw in values.items():
        value = _clean(specs[name], raw)
        if value == "":
            continue
        if saved.get(name) != value:
            saved[name] = value
            changed.append(name)
    for name in clear:
        if name in saved:
            del saved[name]
            changed.append(name)
    encrypted = fernet.encrypt(json.dumps(saved).encode("utf-8")).decode("utf-8")
    async with SessionLocal() as session:
        row = await session.get(BrokerCredential, provider)
        if row is None:
            session.add(
                BrokerCredential(
                    provider=provider,
                    encrypted_access_token=encrypted,
                    expires_at=datetime.max.replace(tzinfo=UTC),
                    updated_by_user_id=user_id,
                )
            )
        else:
            row.encrypted_access_token, row.updated_by_user_id = encrypted, user_id
        await session.commit()
    return sorted(set(changed))


def apply(settings: Settings, provider: str, saved: dict[str, Any]) -> Settings:
    update = {spec.setting: saved[spec.name] for spec in FIELDS[provider] if spec.name in saved}
    return settings.model_copy(update=update) if update else settings


async def effective_settings(settings: Settings) -> Settings:
    """``settings`` with every credential saved on the Connections screen layered over ``.env``.

    Never raises: a database that cannot be read leaves ``.env`` in charge,
    which is what the system ran on before the screen existed.
    """
    from app.services.telegram_config import configured_settings

    try:
        settings = apply(settings, UPSTOX_APP, await load_saved(settings, UPSTOX_APP))
    except Exception:  # noqa: BLE001 - fall back to .env, never stop a login or an alert
        logger.warning("credentials.load_failed", provider=UPSTOX_APP)
    try:
        settings = await configured_settings(settings)
    except Exception:  # noqa: BLE001
        logger.warning("credentials.load_failed", provider="TELEGRAM")
    return settings


def masked(spec: FieldSpec, value: Any) -> str | None:
    """What the screen may see of a value: nothing of a secret, the last characters of an identifier."""
    if value in (None, ""):
        return None
    if spec.kind == "boolean":
        return "On" if value else "Off"
    if spec.kind == "text":
        return str(value)
    if spec.kind == "identifier":
        text = str(value)
        return "•" * 4 + text[-3:] if len(text) > 3 else "•" * 4
    return "Set"


def describe(settings: Settings, provider: str, saved: dict[str, Any]) -> list[dict[str, Any]]:
    """Each field: whether it is set, from where, and what may be shown of it."""
    rows = []
    for spec in FIELDS[provider]:
        env_value = getattr(settings, spec.setting)
        if spec.name in saved:
            source, value = "screen", saved[spec.name]
        elif env_value not in (None, "", False):
            source, value = "server file", env_value
        else:
            source, value = None, env_value
        rows.append(
            {
                "name": spec.name,
                "label": spec.label,
                "kind": spec.kind,
                "help": spec.help,
                "configured": value not in (None, "") if spec.kind != "boolean" else bool(value),
                "source": source,
                "shown": masked(spec, value),
                "value": bool(value) if spec.kind == "boolean" else None,
            }
        )
    return rows
