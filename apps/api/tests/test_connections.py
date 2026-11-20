"""U3: broker and Telegram credentials entered from Settings > Connections.

The claims that matter: nothing saved means every reader sees ``.env`` exactly
as before; what is saved is encrypted, wins over ``.env``, and is never sent
back; a blank box never wipes a saved secret; the morning auto-login reads the
saved values when it runs and stays silent when nothing is configured; and an
audit entry names the fields that changed, never their values.
"""

import inspect
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.routes import connections as routes
from app.core.config import get_settings
from app.db.models import AuditLog, BrokerCredential
from app.db.session import SessionLocal
from app.services import credential_store, scheduler
from app.services.credential_store import UPSTOX_APP, effective_settings
from app.services.telegram_config import PROVIDER as TELEGRAM
from app.services.telegram_config import configured_settings

ADMIN = SimpleNamespace(id=None)
KEY = Fernet.generate_key().decode()
SECRET_PIN, SECRET_TOTP, SECRET_API = "246810", "JBSWY3DPEHPK3PXP", "s3cr3t-api-value"


def env(**overrides):
    base = {
        "upstox_token_encryption_key": KEY,
        "telegram_config_encryption_key": None,
        "upstox_api_key": "env-api-key-123",
        "upstox_api_secret": "env-secret",
        "upstox_redirect_uri": "https://env.example/cb",
        "upstox_mobile_number": None,
        "upstox_pin": None,
        "upstox_totp_secret": None,
        "upstox_auto_auth_enabled": False,
        "telegram_bot_token": "111:env-token",
        "telegram_chat_id": "-100env",
    }
    return get_settings().model_copy(update={**base, **overrides})


@pytest.fixture(autouse=True)
async def no_saved_rows():
    async with SessionLocal() as session:
        rows = (
            await session.scalars(select(BrokerCredential).where(BrokerCredential.provider.in_((UPSTOX_APP, TELEGRAM))))
        ).all()
        backup = [(row.provider, row.encrypted_access_token, row.expires_at) for row in rows]
        await session.execute(delete(BrokerCredential).where(BrokerCredential.provider.in_((UPSTOX_APP, TELEGRAM))))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(BrokerCredential).where(BrokerCredential.provider.in_((UPSTOX_APP, TELEGRAM))))
        for provider, token, expires in backup:
            session.add(BrokerCredential(provider=provider, encrypted_access_token=token, expires_at=expires))
        await session.commit()


async def save_upstox(settings, values=None, clear=None):  # noqa: ANN001
    return await routes.save_upstox(routes.UpstoxSave(values=values or {}, clear=clear or []), settings, ADMIN)


# --- nothing saved: .env, unchanged ----------------------------------------------------


async def test_with_nothing_saved_every_reader_sees_the_env_settings():
    settings = env()
    effective = await effective_settings(settings)
    for name in (
        "upstox_api_key",
        "upstox_api_secret",
        "upstox_redirect_uri",
        "telegram_bot_token",
        "telegram_chat_id",
    ):
        assert getattr(effective, name) == getattr(settings, name)


async def test_without_a_server_key_nothing_can_be_saved_and_env_still_applies():
    settings = env(upstox_token_encryption_key=None)
    with pytest.raises(HTTPException) as refused:
        await save_upstox(settings, {"pin": SECRET_PIN})
    assert refused.value.status_code == 422
    assert "encryption key" in refused.value.detail
    assert (await routes.connections(settings, ADMIN)).encryption_ready is False
    assert (await effective_settings(settings)).upstox_api_key == "env-api-key-123"


# --- saving ------------------------------------------------------------------------------


async def test_saved_values_win_over_env_and_complete_the_auto_login():
    settings = env()
    view = await save_upstox(
        settings,
        {"mobile_number": "9876543210", "pin": SECRET_PIN, "totp_secret": SECRET_TOTP, "auto_auth_enabled": True},
    )
    assert view.upstox.auto_login_ready is True
    effective = await effective_settings(settings)
    assert effective.upstox_pin == SECRET_PIN
    assert effective.upstox_auto_auth_is_configured
    assert effective.upstox_api_key == "env-api-key-123"  # not saved here, so .env still applies


async def test_secrets_are_encrypted_at_rest_and_never_sent_back():
    settings = env()
    view = await save_upstox(settings, {"api_secret": SECRET_API, "pin": SECRET_PIN, "totp_secret": SECRET_TOTP})
    async with SessionLocal() as session:
        stored = (await session.get(BrokerCredential, UPSTOX_APP)).encrypted_access_token
    dumped = view.model_dump_json()
    for secret in (SECRET_API, SECRET_PIN, SECRET_TOTP):
        assert secret not in stored
        assert secret not in dumped
    fields = {field.name: field for field in view.upstox.fields}
    assert fields["api_secret"].shown == "Set" and fields["api_secret"].source == "screen"
    assert fields["api_key"].shown == "••••123" and fields["api_key"].source == "server file"


async def test_a_blank_box_keeps_a_saved_secret_and_clear_hands_back_to_env():
    settings = env()
    await save_upstox(settings, {"api_secret": SECRET_API})
    await save_upstox(settings, {"api_secret": ""})
    assert (await effective_settings(settings)).upstox_api_secret == SECRET_API
    await save_upstox(settings, clear=["api_secret"])
    assert (await effective_settings(settings)).upstox_api_secret == "env-secret"


async def test_the_audit_names_changed_fields_never_values():
    settings = env()
    await routes.save_upstox(routes.UpstoxSave(values={"pin": SECRET_PIN}, reason="new phone"), settings, ADMIN)
    async with SessionLocal() as session:
        entry = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == "connections.upstox_updated")
                .order_by(AuditLog.created_at.desc())
            )
        ).first()
    assert entry.metadata_json == {"changed": ["pin"], "reason": "new phone"}


@pytest.mark.parametrize(
    "values",
    [
        {"pin": "12ab"},
        {"mobile_number": "12345"},
        {"redirect_uri": "ftp://x"},
        {"auto_auth_enabled": "yes"},
        {"nope": "x"},
    ],
)
async def test_malformed_values_are_refused(values):
    with pytest.raises(HTTPException) as refused:
        await save_upstox(env(), values)
    assert refused.value.status_code == 422


# --- telegram ------------------------------------------------------------------------------


async def test_telegram_saved_on_the_screen_is_the_bot_alerts_use_even_without_its_own_key():
    settings = env()
    view = await routes.save_telegram(
        routes.TelegramSave(bot_token="222:screen-token", chat_id="-100screen"), settings, ADMIN
    )
    assert view.telegram.ready
    assert "screen-token" not in view.model_dump_json()
    effective = await configured_settings(settings)
    assert (effective.telegram_bot_token, effective.telegram_chat_id) == ("222:screen-token", "-100screen")


async def test_telegram_chat_id_alone_can_change_keeping_the_token():
    settings = env()
    await routes.save_telegram(routes.TelegramSave(chat_id="-100new"), settings, ADMIN)
    effective = await configured_settings(settings)
    assert (effective.telegram_bot_token, effective.telegram_chat_id) == ("111:env-token", "-100new")


async def test_a_bad_bot_token_is_refused():
    with pytest.raises(HTTPException):
        await routes.save_telegram(routes.TelegramSave(bot_token="not-a-token"), env(), ADMIN)


# --- the morning auto-login ------------------------------------------------------------


async def test_the_morning_job_stays_silent_when_nothing_is_configured(monkeypatch):
    called = []

    async def renewal(*args, **kwargs):  # noqa: ANN002, ANN003
        called.append(args)

    monkeypatch.setattr(scheduler, "run_upstox_auto_renewal", renewal)
    await scheduler._make_job_func(env(), None)()
    assert called == []


async def test_the_morning_job_uses_credentials_saved_after_startup(monkeypatch):
    seen = []

    async def renewal(settings, calendar, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen.append(settings)
        return None

    async def publish(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(scheduler, "run_upstox_auto_renewal", renewal)
    monkeypatch.setattr(scheduler, "_publish_status_to_redis", publish)
    settings = env()
    job = scheduler._make_job_func(settings, None)  # made at startup, before anything was saved
    await save_upstox(
        settings,
        {"mobile_number": "9876543210", "pin": SECRET_PIN, "totp_secret": SECRET_TOTP, "auto_auth_enabled": True},
    )
    await job()
    assert len(seen) == 1 and seen[0].upstox_pin == SECRET_PIN


def test_the_morning_job_is_always_scheduled():
    source = inspect.getsource(scheduler.init_upstox_scheduler)
    assert "if settings.upstox_auto_auth_is_configured:\n        # Primary job" not in source
    assert '"upstox_morning_renewal"' in source


def test_order_placement_does_not_read_the_connections_store():
    from app.services import upstox_orders

    assert "credential_store" not in inspect.getsource(upstox_orders)
    assert credential_store.UPSTOX_APP != "UPSTOX"  # the daily token row is a different row
