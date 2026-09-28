"""Read-only audit of a running deployment, for an operator who is not the author.

Every figure the go-live checklist asks an operator to confirm is somewhere
different: two of them in ``.env``, three behind a UI screen, one in Redis, one
in a database row that overrides the environment file it was copied from. An
operator checking them by hand reads seven places and has no way to notice the
one they skipped.

So this prints all of them together, and says which source each came from.

**It writes nothing and places nothing.** It is safe on a live deployment during
market hours.

**It prints no secret.** A credential is shown as set or unset, never as a
value, because the usual reason to run this is to paste the output to somebody
else.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.models import ApplicationSetting  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

OK = "  [OK]  "
WARN = "  [WARN]"
BAD = "  [PROBLEM]"


def heading(text: str) -> None:
    print("=" * 70)
    print(text)
    print("=" * 70)


def flag(value: bool, good: str, bad: str, *, fatal: bool = True) -> None:
    print(f"{OK if value else (BAD if fatal else WARN)} {good if value else bad}")


async def main() -> int:
    settings = get_settings()
    problems = 0

    heading("1. Runtime mode")
    armed = settings.application_mode == "LIVE" and settings.live_trading_enabled
    print(f"  APPLICATION_MODE         : {settings.application_mode}")
    print(f"  LIVE_TRADING_ENABLED     : {settings.live_trading_enabled}")
    print(f"  LIVE_COMPLIANCE_APPROVED : {settings.live_compliance_approved}")
    print(f"  LIVE_STATIC_IP_VERIFIED  : {settings.live_static_ip_verified}")
    print(f"  LIVE_SHADOW_ENABLED      : {settings.live_shadow_enabled}")
    # Both are needed together, and the one that is set alone is the dangerous
    # reading: it looks armed on this screen while nothing can reach a broker.
    if armed:
        print(f"{WARN} This deployment can place REAL ORDERS once the readiness")
        print("  gates pass. Money is at risk here; the environment is no longer")
        print("  the thing stopping it.")
    elif settings.application_mode == "LIVE" or settings.live_trading_enabled:
        print(f"{WARN} Half-configured: live trading needs APPLICATION_MODE=LIVE")
        print("  and LIVE_TRADING_ENABLED=true together. As set, no order is sent,")
        print("  and the readiness screen's runtime-mode gate stays red.")
    else:
        print(f"{OK} Paper. No order can reach a broker from this configuration.")

    heading("2. Upstox credentials (set / unset only, never values)")
    for label, present in (
        ("api_key", bool(settings.upstox_api_key)),
        ("api_secret", bool(settings.upstox_api_secret)),
        ("token_encryption_key", bool(settings.upstox_token_encryption_key)),
        ("redirect_uri", bool(settings.upstox_redirect_uri)),
    ):
        print(f"  {label:22}: {present}")
    flag(
        settings.upstox_oauth_is_configured,
        "OAuth renewal is configured; 'Renew Access (Web Login)' will work.",
        "OAuth renewal is NOT configured; a token cannot be renewed from the UI.",
    )
    problems += not settings.upstox_oauth_is_configured
    if settings.upstox_access_token:
        print(f"{WARN} UPSTOX_ACCESS_TOKEN is set in .env.")
        print("  With an encryption key present the stored token wins, so this is")
        print("  a stale fallback that turns a clean DB failure into a 401.")
    if settings.upstox_auto_auth_enabled and not settings.upstox_auto_auth_is_configured:
        print(f"{WARN} UPSTOX_AUTO_AUTH_ENABLED is true but the credentials are incomplete.")
        print("  The morning scheduler will no-op silently rather than renew.")

    heading("3. Telegram, in both directions")
    inbound = (
        settings.telegram_is_configured
        and bool(settings.telegram_webhook_url)
        and bool(settings.telegram_webhook_secret)
        and bool(settings.telegram_allowed_users)
    )
    print(f"  bot_token / chat_id   : {settings.telegram_is_configured}")
    print(f"  webhook_url           : {settings.telegram_webhook_url or '(empty)'}")
    print(f"  webhook_secret        : {bool(settings.telegram_webhook_secret)}")
    print(f"  allowed_users         : {settings.telegram_allowed_users or '(EMPTY)'}")
    flag(
        inbound,
        "Inbound approvals are configured.",
        "Inbound approvals are NOT configured.",
    )
    if not inbound:
        problems += 1
        print("  Under TELEGRAM_APPROVAL this blocks every order and reports")
        print("  nothing: an order that cannot be approved is refused, not queued.")
    print("  Config being present does not prove the webhook host is reachable.")
    print("  Send a test alert and tap a button to settle that.")

    async with SessionLocal() as session:
        heading("4. Trading controls (database, set through Settings)")
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls(**(row.value if row else DEFAULT_TRADING_CONTROLS))
        source = "database" if row else "defaults (never saved)"
        print(f"  source                  : {source}")
        print(f"  live_broker             : {controls.live_broker}")
        print(f"  execution_approval_mode : {controls.execution_approval_mode}")
        flag(
            controls.live_broker == "UPSTOX",
            "Live broker is UPSTOX.",
            f"Live broker is {controls.live_broker} - NONE sends nothing.",
        )
        problems += controls.live_broker != "UPSTOX"
        flag(
            controls.execution_approval_mode != "AUTOMATIC",
            f"Approval mode is {controls.execution_approval_mode}.",
            "Approval mode is AUTOMATIC - nobody confirms an order.",
            fatal=False,
        )

        heading("4b. Risk limits, read together")
        capital = controls.account_capital
        per_trade = capital * controls.risk_per_trade_percent / 100
        daily = capital * controls.maximum_daily_risk_percent / 100
        print(f"  account_capital             : {capital:,.2f}")
        print(f"  risk_per_trade_percent      : {controls.risk_per_trade_percent}  (= {per_trade:,.2f})")
        print(f"  maximum_daily_risk_percent  : {controls.maximum_daily_risk_percent}  (= {daily:,.2f})")
        print(f"  maximum_daily_trades        : {controls.maximum_daily_trades}")
        print(f"  maximum_open_positions      : {controls.maximum_open_positions}")
        print(f"  daily_loss_limit            : {controls.daily_loss_limit:,.2f}")
        print(f"  daily_profit_target         : {controls.daily_profit_target:,.2f}")
        print(f"  trade window                : {controls.trade_start_time} - {controls.trade_cutoff_time}")
        print(
            f"  intraday_leverage           : {controls.intraday_leverage_enabled} x{controls.intraday_leverage_multiplier}"
        )
        # Exposure was absent here, which meant the one ceiling that can bind
        # before risk-based sizing was the one figure this audit could not show.
        leverage = controls.intraday_leverage_multiplier if controls.intraday_leverage_enabled else 1.0
        exposure = capital * controls.maximum_open_exposure_percent * leverage / 100
        print(f"  maximum_open_exposure_pct   : {controls.maximum_open_exposure_percent}")
        print(f"  effective exposure ceiling  : {exposure:,.2f}  (capital x pct x {leverage}x)")

        # Planned risk and realised P&L are different quantities, so a loss stop
        # above the daily budget is not wrong. It is worth stating, because an
        # operator who chose "4% a day" has usually chosen a floor, not a budget.
        if controls.daily_loss_limit and capital > 0:
            loss_percent = controls.daily_loss_limit * 100 / capital
            if abs(loss_percent - controls.maximum_daily_risk_percent) > 0.01:
                print(f"  [NOTE] The daily loss stop is {loss_percent:.1f}% of capital while the planned")
                print(f"         risk budget is {controls.maximum_daily_risk_percent}%. The budget stops new")
                print("         trades; the loss stop halts the day on realised P&L.")

        # Capital here is what position sizing divides; a figure copied from a
        # previous account silently resizes every order.
        if capital > 0:
            print("  Check account_capital matches the funded balance: position")
            print("  sizing divides by it, so a stale figure resizes every order.")

        if controls.execution_approval_mode == "TELEGRAM_APPROVAL":
            print()
            print("  [NOTE] Approval mode is TELEGRAM_APPROVAL: every live order")
            print("         waits for a Telegram tap and is refused if unanswered.")
            print("         Arming on a schedule does not make order flow automatic.")

        heading("5. Indicator periods: which source is actually in use")
        from app.services.indicator_settings import INDICATOR_FIELDS, INDICATOR_KEY

        stored = await session.get(ApplicationSetting, INDICATOR_KEY)
        print(f"  source                : {'database' if stored else '.env fallback'}")
        for field in INDICATOR_FIELDS:
            value = (stored.value or {}).get(field) if stored else getattr(settings, field, None)
            print(f"  {field:26}: {value}")

        heading("6. Square-off time, per strategy")
        # Parse through the application's own model rather than reaching into
        # the stored JSON: the row is a bare list, a reader that assumed a
        # wrapper object would be wrong, and a reader that guesses the shape
        # right today breaks silently the next time the shape moves.
        from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration

        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        stored_strategies = row.value if row else DEFAULT_STRATEGIES
        print(f"  source                : {'database' if row else 'shipped defaults'}")
        missing = 0
        for item in stored_strategies:
            try:
                configuration = StrategyConfiguration.model_validate(item)
            except Exception as exc:  # a row this cannot parse is itself the finding
                problems += 1
                print(f"{BAD} unparseable strategy row: {exc}")
                continue
            square_off = configuration.exit_rules.square_off_time
            name = configuration.name[:34]
            if square_off:
                print(f"{OK} {name:34} {square_off}  enabled={configuration.enabled}")
            else:
                missing += 1
                print(f"{BAD} {name:34} NO SQUARE-OFF  enabled={configuration.enabled}")
        problems += missing
        if missing:
            print("  Nothing closes a position because the session is ending.")
            print("  Live, the broker squares off MIS at its own time and price.")

    heading("SUMMARY")
    if problems:
        print(f"  {problems} item(s) need attention before live trading.")
    else:
        print("  No blocking problem found in the configuration this can see.")
    print("  Not covered here: whether the registered static IP matches the")
    print("  outbound address, whether the account is funded, and whether")
    print("  placement is permitted. Only verify_upstox_orders.py settles those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
