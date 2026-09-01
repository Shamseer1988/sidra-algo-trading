"""Automated Upstox daily OAuth authentication service using TOTP.

Enables headless, cron-driven renewal of daily market data tokens and
sends status notifications via Telegram.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
import structlog
from playwright.async_api import async_playwright

from app.core.config import Settings
from app.services.telegram import TelegramError, TelegramNotificationService
from app.services.upstox_oauth import (
    UpstoxOAuthError,
    exchange_authorization_code,
    store_access_token,
)

logger = structlog.get_logger("upstox_auto_auth")

UPSTOX_AUTH_ENDPOINT = "https://api.upstox.com/v2/login/authorization/dialog"


@dataclass(frozen=True)
class UpstoxAutoAuthResult:
    success: bool
    message: str
    expires_at: datetime | None = None
    token_masked: str | None = None


class UpstoxAutoAuthService:
    """Automates daily Upstox authentication using TOTP and Playwright headless login."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._telegram = TelegramNotificationService(settings) if settings.telegram_is_configured else None

    def generate_current_totp(self) -> str:
        """Generate current 6-digit TOTP using configured secret key."""
        if not self._settings.upstox_totp_secret:
            raise UpstoxOAuthError("UPSTOX_TOTP_SECRET is not configured")
        try:
            totp = pyotp.TOTP(self._settings.upstox_totp_secret.strip())
            return totp.now()
        except Exception as exc:
            raise UpstoxOAuthError(f"Failed to generate TOTP: {exc}") from exc

    async def execute_automated_login(self, auth_code_override: str | None = None) -> UpstoxAutoAuthResult:
        """Execute automated authentication flow and store renewed access token."""
        if not self._settings.upstox_auto_auth_is_configured and not auth_code_override:
            raise UpstoxOAuthError(
                "Upstox auto-authentication settings are incomplete. "
                "Ensure UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_MOBILE_NUMBER, "
                "UPSTOX_PIN, UPSTOX_TOTP_SECRET, and UPSTOX_TOKEN_ENCRYPTION_KEY are set."
            )

        logger.info("upstox.auto_auth_initiated")

        code = auth_code_override
        if not code:
            code = await self._obtain_auth_code()

        # Step 2: Exchange authorization code for fresh access token
        access_token = await exchange_authorization_code(self._settings, code)

        # Step 3: Persist encrypted token to database
        expires_at = await store_access_token(self._settings, access_token, updated_by_user_id=None)

        masked_token = f"{access_token[:4]}...{access_token[-4:]}" if len(access_token) > 8 else "***"
        ist_time = expires_at.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d-%b-%Y %I:%M %p")

        logger.info("upstox.auto_auth_success", expires_at=expires_at.isoformat())

        # Step 4: Dispatch Telegram success notification
        if self._telegram:
            notification_text = (
                "🟢 *[Intraday Sentinel] Upstox Auth Renewed*\n\n"
                f"• *Status*: ✅ Success\n"
                f"• *Token*: `{masked_token}`\n"
                f"• *Valid Until*: `{ist_time} IST`\n"
                f"• *Market Data Feed*: Ready for session\n\n"
                "_Automated via Daily Scheduled Auth Job_"
            )
            try:
                await self._telegram.send_message(notification_text)
            except TelegramError as exc:
                logger.warning("upstox.auto_auth_telegram_failed", error=str(exc))

        return UpstoxAutoAuthResult(
            success=True,
            message="Upstox token renewed successfully",
            expires_at=expires_at,
            token_masked=masked_token,
        )

    async def _obtain_auth_code(self) -> str:
        """Perform headless browser authentication via Playwright to retrieve authorization code."""
        totp_code = self.generate_current_totp()
        mobile = (self._settings.upstox_mobile_number or "").strip()
        pin = (self._settings.upstox_pin or "").strip()
        auth_url = (
            f"{UPSTOX_AUTH_ENDPOINT}?response_type=code"
            f"&client_id={self._settings.upstox_api_key}"
            f"&redirect_uri={self._settings.upstox_redirect_uri}"
        )

        auth_code_holder: list[str] = []

        def check_for_code(url: str) -> None:
            if "code=" in url:
                val = url.split("code=")[1].split("&")[0].split("#")[0]
                if val and val not in auth_code_holder:
                    auth_code_holder.append(val)
                    logger.info("upstox.auth_code_captured", code=f"{val[:4]}...{val[-4:]}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            page.on("framenavigated", lambda frame: check_for_code(frame.url))
            page.on("response", lambda resp: check_for_code(resp.url))

            try:
                logger.info("upstox.navigating_to_login", url=auth_url)
                await page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # Step 1: Fill Mobile Number
                mobile_input = page.locator('input[type="tel"], #mobileNum, input[name="mobileNumber"]').first
                await mobile_input.wait_for(state="visible", timeout=15000)
                await mobile_input.fill(mobile)
                await page.wait_for_timeout(500)

                # Click Get OTP button
                get_otp_btn = page.locator('button:has-text("Get OTP"), #getOtp, button[type="submit"]').first
                await get_otp_btn.click()
                await page.wait_for_timeout(2000)

                # Check if there is a button/link to switch from SMS OTP to TOTP (Authenticator app)
                totp_switch = page.locator('text=/TOTP|Authenticator|Use TOTP|Login with TOTP|Verify with TOTP/i').first
                if await totp_switch.count() > 0:
                    try:
                        if await totp_switch.is_visible():
                            logger.info("upstox.switching_to_totp_mode")
                            await totp_switch.click()
                            await page.wait_for_timeout(1000)
                    except Exception:
                        pass

                # Step 2: Fill TOTP Code
                logger.info("upstox.filling_totp")
                digit_inputs = page.locator('input[type="tel"], input[type="number"], input[type="text"]')
                input_count = await digit_inputs.count()
                if input_count >= 6:
                    for idx, char in enumerate(totp_code):
                        await digit_inputs.nth(idx).fill(char)
                else:
                    otp_input = digit_inputs.first
                    await otp_input.wait_for(state="visible", timeout=15000)
                    await otp_input.fill(totp_code)

                await page.wait_for_timeout(500)

                # Click Continue button
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Verify"), button[type="submit"]').first
                if await continue_btn.count() > 0 and await continue_btn.is_visible():
                    await continue_btn.click()

                await page.wait_for_timeout(2000)

                # Step 3: Fill 6-digit PIN
                logger.info("upstox.filling_pin")
                pin_digit_inputs = page.locator('input[type="password"], input[type="tel"], input[type="number"]')
                pin_count = await pin_digit_inputs.count()
                if pin_count >= 6:
                    for idx, char in enumerate(pin):
                        await pin_digit_inputs.nth(idx).fill(char)
                else:
                    pin_input = page.locator('input[type="password"], #pinCode, input[name="pin"]').first
                    if await pin_input.count() > 0:
                        await pin_input.wait_for(state="visible", timeout=15000)
                        await pin_input.fill(pin)

                await page.wait_for_timeout(500)

                # Click Submit / Continue PIN button
                submit_btn = page.locator(
                    'button:has-text("Continue"), button:has-text("Submit"), button[type="submit"]'
                ).first
                if await submit_btn.count() > 0 and await submit_btn.is_visible():
                    await submit_btn.click()

                # Step 4: Check if an "Authorize" / "Allow" OAuth permission screen appears
                await page.wait_for_timeout(2000)
                allow_btn = page.locator('button:has-text("Authorize"), button:has-text("Allow"), button:has-text("Accept"), button:has-text("Continue")').first
                if await allow_btn.count() > 0 and await allow_btn.is_visible():
                    logger.info("upstox.clicking_authorize_button")
                    await allow_btn.click()

                # Step 5: Wait for redirect
                logger.info("upstox.waiting_for_auth_code_redirect", current_url=page.url)
                for _ in range(40):
                    if auth_code_holder:
                        break
                    check_for_code(page.url)
                    # Also check if another Continue button is on the page
                    cont = page.locator('button:has-text("Continue"), button:has-text("Authorize")').first
                    if await cont.count() > 0 and await cont.is_visible():
                        try:
                            await cont.click()
                        except Exception:
                            pass
                    await page.wait_for_timeout(500)

                if not auth_code_holder:
                    logger.warning("upstox.headless_login_timeout", final_url=page.url)
                    logs_dir = Path("/app/logs")
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(logs_dir / "upstox_auth_debug.png"))

            except Exception as exc:
                check_for_code(page.url)
                if not auth_code_holder:
                    logs_dir = Path("/app/logs")
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(logs_dir / "upstox_auth_debug.png"))
                    raise UpstoxOAuthError(f"Headless login step failed: {exc}") from exc
            finally:
                await browser.close()

        if not auth_code_holder:
            raise UpstoxOAuthError("Headless login completed without capturing authorization code")

        return auth_code_holder[0]
