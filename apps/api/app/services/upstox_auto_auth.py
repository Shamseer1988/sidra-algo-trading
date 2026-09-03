"""Headless Upstox OAuth auto-login via mobile OTP + TOTP + PIN.

Performs the full Upstox login sequence without a browser:
  1. GET /v2/login/authorization/dialog → extracts user_id and client_id from redirect URL
  2. POST /login/open/v6/auth/1fa/otp/generate → receives validateOTPToken
  3. POST /login/open/v4/auth/1fa/otp-totp/verify → validates OTP with pyotp TOTP
  4. POST /login/open/v3/auth/2fa → verifies base64-encoded PIN
  5. POST /login/v2/oauth/authorize → receives redirectUri with auth code
  6. POST /v2/login/authorization/token → exchanges code for access token
  7. Stores encrypted token in DB

Designed to be called by the morning scheduler at 08:30 AM IST on every
business day so the scanner worker always has a fresh token before market
pre-open.
"""

from __future__ import annotations

import base64
import random
import string
import urllib.parse
from datetime import UTC, datetime

import httpx
import pyotp
import structlog

from app.core.config import Settings
from app.services.upstox_oauth import exchange_authorization_code, store_access_token

logger = structlog.get_logger("upstox.auto_auth")

UPSTOX_INTERNAL_REDIRECT = "https://api-v2.upstox.com/login/authorization/redirect"
UPSTOX_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_OTP_GENERATE_URL = "https://service.upstox.com/login/open/v6/auth/1fa/otp/generate"
UPSTOX_OTP_VERIFY_URL = "https://service.upstox.com/login/open/v4/auth/1fa/otp-totp/verify"
UPSTOX_PIN_VERIFY_URL = "https://service.upstox.com/login/open/v3/auth/2fa"
UPSTOX_OAUTH_AUTHORIZE_URL = "https://service.upstox.com/login/v2/oauth/authorize"


class UpstoxAutoAuthError(RuntimeError):
    """Raised when headless auto-login fails at any step."""


def _generate_request_id() -> str:
    return "WPRO-" + "".join(random.choices(string.ascii_letters + string.digits, k=10))


def _build_headers(request_id: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-GB,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://login.upstox.com",
        "referer": "https://login.upstox.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "x-device-details": "platform=WEB|osName=Mac OS/10.15.7|osVersion=Chrome/140.0.0.0|appVersion=4.0.0|modelName=Chrome|manufacturer=Apple|uuid=3Z1IVTlV4rUUGbNp8KP0|userAgent=Upstox 3.0 Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "x-request-id": request_id,
    }


async def perform_auto_login(settings: Settings) -> dict:
    """Run the full headless Upstox login and return renewal metadata.

    Returns a dict with:
        access_token – the new plain-text token
        expires_at   – ISO-format expiry datetime
        renewed_at   – ISO-format timestamp of this renewal
    """
    if not settings.upstox_auto_auth_is_configured:
        raise UpstoxAutoAuthError(
            "Auto-auth is not fully configured — check UPSTOX_MOBILE_NUMBER, "
            "UPSTOX_PIN, UPSTOX_TOTP_SECRET and OAuth settings in .env"
        )

    mobile = settings.upstox_mobile_number
    pin = settings.upstox_pin
    totp_secret = settings.upstox_totp_secret
    client_id = settings.upstox_api_key
    redirect_uri = str(settings.upstox_redirect_uri)

    logger.info("upstox.auto_auth.starting", mobile=mobile[-4:] if mobile else "?")

    request_id = _generate_request_id()
    headers = _build_headers(request_id)

    async with httpx.AsyncClient(headers=headers, timeout=25.0, follow_redirects=True) as client:
        # ── Step 1: Dialog request to obtain user_id and actual client_id ──
        dialog_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
        try:
            res_dialog = await client.get(UPSTOX_DIALOG_URL, params=dialog_params)
            res_dialog.raise_for_status()
        except Exception as exc:
            raise UpstoxAutoAuthError(f"Step 1 dialog request failed: {exc}") from exc

        parsed_dialog_url = urllib.parse.urlparse(str(res_dialog.url))
        query_params = urllib.parse.parse_qs(parsed_dialog_url.query)
        user_id = query_params.get("user_id", [None])[0]
        actual_client_id = query_params.get("client_id", [client_id])[0]

        if not user_id:
            raise UpstoxAutoAuthError(
                f"Step 1 dialog request did not return a user_id in redirect: {res_dialog.url}"
            )
        logger.info("upstox.auto_auth.user_id_obtained", user_id_prefix=user_id[:6] + "…")

        # ── Step 2: Generate OTP ───────────────────────────────────────────
        otp_payload = {
            "data": {
                "mobileNumber": mobile,
                "userId": user_id,
            }
        }
        try:
            res_otp = await client.post(UPSTOX_OTP_GENERATE_URL, json=otp_payload)
            res_otp.raise_for_status()
            otp_data = res_otp.json()
        except Exception as exc:
            raise UpstoxAutoAuthError(f"Step 2 OTP generate request failed: {exc}") from exc

        validate_token = otp_data.get("data", {}).get("validateOTPToken")
        if not validate_token:
            raise UpstoxAutoAuthError(f"Step 2 OTP generation failed: {otp_data}")
        logger.info("upstox.auto_auth.otp_generated")

        # ── Step 3: Verify OTP with pyotp TOTP ────────────────────────────
        totp_code = pyotp.TOTP(totp_secret).now()
        verify_payload = {
            "data": {
                "otp": totp_code,
                "validateOtpToken": validate_token,
            }
        }
        try:
            res_verify = await client.post(UPSTOX_OTP_VERIFY_URL, json=verify_payload)
            res_verify.raise_for_status()
            verify_data = res_verify.json()
        except Exception as exc:
            raise UpstoxAutoAuthError(f"Step 3 OTP-TOTP verify request failed: {exc}") from exc

        if not verify_data.get("success"):
            raise UpstoxAutoAuthError(f"Step 3 OTP-TOTP verification rejected: {verify_data}")
        logger.info("upstox.auto_auth.totp_verified")

        # ── Step 4: 2FA PIN Submission ─────────────────────────────────────
        pin_encoded = base64.b64encode(pin.encode("utf-8")).decode("utf-8")
        pin_params = {
            "client_id": actual_client_id,
            "redirect_uri": UPSTOX_INTERNAL_REDIRECT,
        }
        pin_payload = {
            "data": {
                "twoFAMethod": "SECRET_PIN",
                "inputText": pin_encoded,
            }
        }
        try:
            res_pin = await client.post(UPSTOX_PIN_VERIFY_URL, params=pin_params, json=pin_payload)
            res_pin.raise_for_status()
            pin_data = res_pin.json()
        except Exception as exc:
            raise UpstoxAutoAuthError(f"Step 4 PIN verify request failed: {exc}") from exc

        if not pin_data.get("success"):
            raise UpstoxAutoAuthError(f"Step 4 PIN verification rejected: {pin_data}")
        logger.info("upstox.auto_auth.pin_verified")

        # ── Step 5: OAuth Authorization Code ──────────────────────────────
        auth_params = {
            "client_id": actual_client_id,
            "redirect_uri": UPSTOX_INTERNAL_REDIRECT,
            "requestId": request_id,
            "response_type": "code",
        }
        auth_payload = {
            "data": {
                "userOAuthApproval": True,
            }
        }
        try:
            res_auth = await client.post(UPSTOX_OAUTH_AUTHORIZE_URL, params=auth_params, json=auth_payload)
            res_auth.raise_for_status()
            auth_data = res_auth.json()
        except Exception as exc:
            raise UpstoxAutoAuthError(f"Step 5 OAuth authorize request failed: {exc}") from exc

        redirect_uri_val = auth_data.get("data", {}).get("redirectUri")
        if not redirect_uri_val:
            raise UpstoxAutoAuthError(f"Step 5 OAuth authorization returned no redirectUri: {auth_data}")

        parsed_redirect = urllib.parse.urlparse(redirect_uri_val)
        auth_code_list = urllib.parse.parse_qs(parsed_redirect.query).get("code")
        if not auth_code_list:
            raise UpstoxAutoAuthError(f"No authorization code in redirectUri: {redirect_uri_val}")

        auth_code = auth_code_list[0]
        logger.info("upstox.auto_auth.auth_code_obtained", code_prefix=auth_code[:6] + "…")

        # ── Step 6 & 7: Exchange code for token and save encrypted in DB ──
        access_token = await exchange_authorization_code(settings, auth_code)
        expires_at = await store_access_token(settings, access_token, updated_by_user_id=None)

        renewed_at = datetime.now(UTC)
        logger.info(
            "upstox.auto_auth.success",
            expires_at=expires_at.isoformat(),
            renewed_at=renewed_at.isoformat(),
        )
        return {
            "access_token": access_token,
            "expires_at": expires_at.isoformat(),
            "renewed_at": renewed_at.isoformat(),
        }
