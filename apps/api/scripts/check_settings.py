"""Every setting that governs a live trade, read back and checked against itself.

``audit_deployment.py`` answers "is this deployment capable of trading". This
answers a narrower question an operator asks far more often: **are the numbers
I set actually the numbers that will be used, and do they agree with each
other?**

The difference matters because the settings interact. A daily budget below
(risk per trade x trade ceiling) silently caps the number of trades. A loss
stop smaller than the worst case of several positions open at once can be
overshot by a single market move before it can latch. A per-strategy value
overrides an account one without saying so. None of those is an error anywhere;
each is a number quietly meaning something other than what was typed.

Read-only. It changes nothing and is safe to run during a session.
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ApplicationSetting  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

OK, WARN, BAD = (
    "[OK]  ",
    "[WARN]",
    "[BAD] ",
)


def line(flag: str, text: str) -> None:
    print(f"  {flag} {text}")


def heading(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


async def main() -> int:
    problems = 0
    warnings = 0

    async with SessionLocal() as session:
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
        from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration

        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls(**(row.value if row else DEFAULT_TRADING_CONTROLS))
        capital = Decimal(str(controls.account_capital))

        heading("1. What one trade risks")
        per_trade = capital * Decimal(str(controls.risk_per_trade_percent)) / 100
        print(f"  account_capital            : {capital:,.2f}")
        print(f"  risk_per_trade_percent     : {controls.risk_per_trade_percent}  = {per_trade:,.2f} a trade")
        print(f"  minimum_rr (account)       : {controls.minimum_rr}")
        print(f"  stop_atr_multiple          : {controls.stop_atr_multiple}")
        print(f"  min_stop_distance_percent  : {controls.min_stop_distance_percent}")
        floor = Decimal(str(controls.min_stop_distance_percent)) / 100
        if floor > 0:
            print(f"  -> a stop this wide sizes every trade to {per_trade / floor:,.0f} of exposure")

        heading("2. What the day allows, read together")
        budget = capital * Decimal(str(controls.maximum_daily_risk_percent)) / 100
        ceiling = controls.maximum_daily_trades
        funded = int(budget / per_trade) if per_trade > 0 else 0
        print(f"  maximum_daily_risk_percent : {controls.maximum_daily_risk_percent}  = {budget:,.2f}")
        print(f"  maximum_daily_trades       : {ceiling}")
        print(f"  maximum_open_positions     : {controls.maximum_open_positions}")
        print(f"  daily_loss_limit           : {controls.daily_loss_limit:,.2f}")
        print(f"  daily_profit_target        : {controls.daily_profit_target:,.2f}")
        print(f"  trade window               : {controls.trade_start_time} - {controls.trade_cutoff_time}")

        # The budget silently capping the ceiling is the oldest trap here: the
        # operator sets 3 trades, gets 2, and nothing says why.
        if funded < ceiling:
            warnings += 1
            line(WARN, f"The budget funds {funded} trades but the ceiling says {ceiling}.")
            line(
                "      ",
                f"Set maximum_daily_risk_percent to {controls.risk_per_trade_percent * ceiling} for {ceiling}.",
            )
        else:
            line(OK, f"Budget funds {funded} trades; the ceiling of {ceiling} is what binds.")

        # Several positions open at once share one market. Planned risk per
        # trade stops being the worst case the moment this is above one.
        concurrent = controls.maximum_open_positions
        worst_case = per_trade * concurrent
        if concurrent > 1:
            line(WARN, f"{concurrent} positions may be open at once, so one adverse move can cost {worst_case:,.2f}.")
            if Decimal(str(controls.daily_loss_limit)) < worst_case:
                problems += 1
                line(BAD, f"daily_loss_limit {controls.daily_loss_limit:,.2f} is BELOW that worst case.")
                line("      ", "The day's stop can be overshot before it latches. Raise it, or lower")
                line("      ", "maximum_open_positions back to 1.")
            else:
                line(OK, f"daily_loss_limit {controls.daily_loss_limit:,.2f} covers it.")
        else:
            line(OK, "One position at a time; planned risk per trade is the worst case.")

        full_loss = per_trade * ceiling
        if Decimal(str(controls.daily_loss_limit)) > full_loss:
            warnings += 1
            line(
                WARN,
                f"The loss stop ({controls.daily_loss_limit:,.2f}) is above {ceiling} full stops ({full_loss:,.2f}).",
            )
            line("      ", "The trade ceiling will end the day first; the loss stop will never fire.")

        heading("3. Exposure")
        leverage = (
            Decimal(str(controls.intraday_leverage_multiplier)) if controls.intraday_leverage_enabled else Decimal(1)
        )
        exposure = capital * Decimal(str(controls.maximum_open_exposure_percent)) * leverage / 100
        print(f"  intraday_leverage          : {controls.intraday_leverage_enabled} x{leverage}")
        print(f"  maximum_open_exposure_pct  : {controls.maximum_open_exposure_percent}")
        print(f"  exposure ceiling           : {exposure:,.2f}")
        if floor > 0:
            sized = (per_trade / floor) * concurrent
            if sized <= exposure:
                line(OK, f"Risk sizing caps you at {sized:,.2f}; the exposure ceiling never binds.")
            else:
                line(WARN, f"Risk sizing wants {sized:,.2f}; the exposure ceiling will cut position size.")

        heading("4. Execution")
        print(f"  live_broker                : {controls.live_broker}")
        print(f"  execution_approval_mode    : {controls.execution_approval_mode}")
        print(f"  live_entry_order_type      : {controls.live_entry_order_type}")
        print(f"  entry_slippage_cap_percent : {controls.entry_slippage_cap_percent}")
        if controls.live_broker == "NONE":
            problems += 1
            line(BAD, "No broker selected; live orders have nowhere to go.")
        if controls.execution_approval_mode == "AUTOMATIC":
            line(WARN, "AUTOMATIC: orders are placed without being shown to you first.")
        if controls.live_entry_order_type == "MARKET":
            problems += 1
            line(
                BAD,
                "MARKET entries have no price cap, so the risk on a trade is decided by whatever "
                "the market does between the signal and the fill. On 5 October that put 209 behind "
                "a 100 budget. Set LIMIT to make the planned risk a ceiling.",
            )
        else:
            cap = Decimal(str(controls.entry_slippage_cap_percent))
            line(
                OK,
                f"LIMIT entries fill at most {cap}% past the signal and are sized from that price, "
                f"so a filled trade risks no more than {per_trade:,.2f}.",
            )
            if cap == 0:
                line(WARN, "A 0% cap only fills at the signal's exact price; expect very few fills.")
            elif cap >= 1:
                line(WARN, f"A {cap}% cap is wide; positions will shrink a lot to stay inside the budget.")

        heading("5. What each strategy will actually use")
        row = await session.get(ApplicationSetting, STRATEGIES_KEY)
        stored = row.value if row else DEFAULT_STRATEGIES
        base = controls.model_dump()
        for item in stored:
            try:
                configuration = StrategyConfiguration.model_validate(item)
            except Exception as exc:  # a row that will not parse is the finding
                problems += 1
                line(BAD, f"unreadable strategy row: {exc}")
                continue
            effective = configuration.effective_controls(base)
            name = configuration.name[:30]
            square_off = configuration.exit_rules.square_off_time or "NONE"
            flag = OK if configuration.exit_rules.square_off_time else BAD
            if not configuration.exit_rules.square_off_time:
                problems += 1
            line(flag, f"{name:30} enabled={configuration.enabled}  square-off={square_off}")
            for label, key in (
                ("rr", "minimum_rr"),
                ("atr", "stop_atr_multiple"),
                ("floor%", "min_stop_distance_percent"),
            ):
                value = effective.get(key)
                source = "account" if value == base.get(key) else f"OVERRIDE (account {base.get(key)})"
                print(f"         {label:7}: {value}  {source}")

    heading("SUMMARY")
    if problems:
        print(f"  {problems} setting(s) contradict each other or are missing.")
    if warnings:
        print(f"  {warnings} worth reading before the next session.")
    if not problems and not warnings:
        print("  Every setting agrees with every other one this can compare.")
    print("\n  Not covered: whether the capital figure matches your funded balance,")
    print("  and whether the broker will accept an order. Only a real order settles those.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
