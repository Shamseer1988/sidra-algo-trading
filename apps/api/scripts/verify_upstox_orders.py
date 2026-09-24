"""Check that this Upstox app can actually place an order, before real money.

Everything in the live layer is built on assumptions that no amount of local
testing can settle, because they are facts about your Upstox account rather than
about this code. Four of them decide whether live trading works at all:

  1. **The app is permitted to place orders.** A Developer App set up for market
     data streams quotes perfectly well and refuses placements. Nothing in the
     configuration distinguishes the two — the failure arrives on the first real
     order, which is the worst possible moment to discover it.

  2. **Orders originate from a registered static IP.** Since 1 April 2026 the
     exchanges require every API order to come from an address registered with
     the broker in advance; orders from anywhere else are rejected outright.
     This script tells you the address it is calling from, which is the one that
     has to be registered — not your laptop's, not the NAS's LAN address.

  3. **The order book echoes the ``tag`` we send.** Recovery from an UNKNOWN
     submission depends entirely on it. If it does not come back, an order whose
     response we lost cannot be identified except by guessing from symbol and
     quantity — which cannot tell our order apart from a second one like it.
     Upstox documents ``tag`` as a field of every order record, so this is
     expected to pass; it is checked because "documented" and "true of your
     account today" are different claims.

  4. **The margin endpoints answer in a readable form.** The live risk engine
     refuses any order whose margin it cannot read, so an unreadable response is
     a system that never trades rather than one that trades badly.

The script runs in two stages, and the difference matters:

    STAGE 1 (default)   Reads only. Cannot create, modify or cancel anything.
                        It settles 2 and 4 outright, and settles 3 *if* your
                        account already has orders in today's book. It cannot
                        settle 1 at all: reading is not placing, and only a
                        placement proves a placement is permitted.

    STAGE 2 (opt-in)    Places one real order to settle 1 and 3 definitively,
                        then cancels it. This is a real order with real money at
                        a real exchange. It is off unless you pass
                        --place-test-order and type the confirmation, and it is
                        designed to be unfillable — but "designed to be" is not
                        "cannot be", so only run it with a price far from the
                        market and a quantity you would not mind filling.

Stage 2 talks to the broker directly and therefore does not pass through the
application's live-trading lock. That is deliberate — it is a diagnostic, not a
trading path — but it is the reason the confirmation is explicit.

Usage, from the API container:

    docker compose exec api python scripts/verify_upstox_orders.py
    docker compose exec api python scripts/verify_upstox_orders.py \
        --place-test-order --instrument-key "NSE_EQ|INE669E01016" --price 1.00
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Runnable both inside the container, where the package is installed on
# PYTHONPATH, and from a checkout, where it is not. Diagnostics that only run in
# one of those places tend not to get run.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.broker_adapter import UPSTOX_CLIENT_ID_KEYS, UpstoxAdapter  # noqa: E402
from app.services.live_order_recovery import match_submission  # noqa: E402
from app.services.live_orders import new_client_order_id  # noqa: E402
from app.services.upstox_orders import (  # noqa: E402
    UpstoxApiError,
    UpstoxError,
    UpstoxOrderClient,
    UpstoxSession,
)

CONFIRMATION = "PLACE A REAL ORDER"


def heading(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def field_names(records: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for record in records:
        if isinstance(record, dict):
            names.update(str(key) for key in record)
    return sorted(names)


async def report_egress_ip() -> str | None:
    """The address the exchange will see. This is what must be registered.

    Reported first because it is the cheapest thing to get wrong: registering
    the NAS's LAN address, or the address of the laptop you configured it from,
    produces rejections that say nothing about IPs.
    """
    heading("0. Which IP address do orders leave from?")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.ipify.org")
            address = response.text.strip()
    except Exception as exc:  # any network failure; this is a diagnostic
        print(f"  [WARN] Could not determine the outbound address: {exc}")
        print("  Find it another way before registering anything.")
        return None

    print(f"  Outbound address: {address}")
    print("  This exact address must be the Primary (or Secondary) static IP")
    print("  registered against your app at account.upstox.com/developer/apps.")
    print("  If it is not, every order will be rejected from 1 April 2026 onward.")
    return address


def report_tag_support(order_book: list[dict[str, Any]]) -> bool | None:
    """True, False, or None for "the book was empty so we could not tell"."""
    if not order_book:
        print("  The order book is empty, so this cannot be answered by reading alone.")
        print("  Either run this again on a day with orders, or use stage 2.")
        return None

    present = [key for key in UPSTOX_CLIENT_ID_KEYS if any(key in record for record in order_book)]
    print(f"  Fields present on order records: {', '.join(field_names(order_book))}")
    if present:
        print(f"  [OK] The order book carries {', '.join(present)}. Automatic recovery works.")
        return True
    print("  [PROBLEM] No tag field came back. An order whose response we lose")
    print("  could not be identified automatically, and would have to be")
    print("  resolved by hand before this system traded that instrument again.")
    return False


async def stage_one(client: UpstoxOrderClient, *, instrument_key: str, price: float) -> dict[str, Any]:
    findings: dict[str, Any] = {}

    findings["egress_ip"] = await report_egress_ip()

    heading("1. Does the order book echo the tag we send?")
    order_book = await client.order_book()
    print(f"  {len(order_book)} order(s) in today's book.")
    findings["tag_supported"] = report_tag_support(order_book)

    heading("2. Positions: is the net quantity parseable?")
    positions = await client.positions()
    if not positions:
        print("  No open positions, so nothing to parse. Re-run while holding one.")
    else:
        print(f"  Fields present: {', '.join(field_names(positions))}")
        for record in await UpstoxAdapter(client).normalised_positions():
            state = "unreadable" if record.net_quantity is None else str(record.net_quantity)
            print(f"  {record.symbol}: quantity={record.raw.get('quantity')!r} -> {state}")
    findings["positions"] = len(positions)

    heading("3. Margin: can the live risk engine read an answer?")
    if not instrument_key:
        print("  Skipped: pass --instrument-key to exercise this.")
        findings["margin"] = "skipped"
    else:
        try:
            quote = await client.order_margin(
                instrument_token=instrument_key,
                quantity=1,
                product="I",
                transaction_type="BUY",
                price=price or 1.0,
            )
            funds = await client.funds_and_margin("SEC")
            equity = funds.get("equity") if isinstance(funds, dict) else None
            required = quote.get("final_margin")
            available = equity.get("available_margin") if isinstance(equity, dict) else None
            print(f"  final_margin:     {required!r}")
            print(f"  required_margin:  {quote.get('required_margin')!r}")
            print(f"  available_margin: {available!r}")
            readable = required is not None and available is not None
            print(f"  [{'OK' if readable else 'PROBLEM'}] Both figures are {'' if readable else 'not '}readable.")
            findings["margin"] = "readable" if readable else "unreadable"
        except UpstoxError as exc:
            print(f"  [PROBLEM] Margin call failed: {exc}")
            print("  The live risk engine refuses any order it cannot price, so")
            print("  this blocks trading until it is fixed.")
            findings["margin"] = "failed"

    heading("4. Trade reports: do the History screen's two sources answer?")
    # The History reconciliation compares our figures against these. The
    # request parameters — the dd-mm-yyyy dates and the financial year in
    # particular — are the kind of contract detail that is worth confirming
    # against a real account rather than trusting from documentation.
    from datetime import UTC, datetime, timedelta

    from app.services.broker_day_figures import financial_year, read_charges, read_profit_loss

    window_end = datetime.now(UTC).date()
    window_start = window_end - timedelta(days=30)
    year = financial_year(window_end)
    print(f"  Asking for {window_start} to {window_end}, financial year {year}.")
    try:
        rows = await client.trade_profit_loss(
            from_date=window_start, to_date=window_end, financial_year=year, page_number=1, page_size=100
        )
        realised, turnover, count = read_profit_loss(rows)
        print(f"  [OK] profit-loss/data returned {len(rows)} row(s).")
        if rows:
            print(f"  fields: {', '.join(field_names(rows))}")
            print(f"  realised: {realised!r}  turnover: {turnover!r}  matched pairs: {count}")
        else:
            print("  No rows. Either nothing traded in this window, or the")
            print("  financial year or date format is not what this expects.")
        findings["profit_loss"] = f"{len(rows)} rows"
    except UpstoxError as exc:
        print(f"  [PROBLEM] profit-loss/data failed: {exc}")
        print("  The History screen will stay on BROKER DATA PENDING until this works.")
        findings["profit_loss"] = "failed"

    try:
        charges_body = await client.trade_charges(from_date=window_start, to_date=window_end, financial_year=year)
        total = read_charges(charges_body)
        print(f"  [{'OK' if total is not None else 'WARN'}] profit-loss/charges total: {total!r}")
        if total is None and charges_body:
            print(f"  body keys: {', '.join(sorted(charges_body))}")
            print("  The total could not be read from this shape; read_charges needs adjusting.")
        print("  Note: this figure is for the whole window, not per trade. Upstox")
        print("  publishes no per-trade charge, which is why per-trade costs in")
        print("  this system are estimated locally and always will be.")
        findings["charges"] = "readable" if total is not None else "unreadable"
    except UpstoxError as exc:
        print(f"  [PROBLEM] profit-loss/charges failed: {exc}")
        findings["charges"] = "failed"

    heading("5. Failure envelope: does a bad request classify as documented?")
    try:
        await client.order_details("definitely-not-an-order-id")
        print("  [WARN] A nonsense order id did not produce an error.")
        print("  The client's refusal handling could not be exercised.")
        findings["envelope"] = "unverified"
    except UpstoxError as exc:
        print(f"  [OK] Refused as {type(exc).__name__}: {exc}")
        findings["envelope"] = type(exc).__name__

    return findings


async def stage_two(client: UpstoxOrderClient, *, instrument_key: str, price: float, quantity: int) -> str:
    """Place one real order, find it by our tag, cancel it. Returns a verdict."""
    client_order_id = new_client_order_id()

    heading("STAGE 2: placing one real order")
    print(f"  Instrument  {instrument_key}")
    print(f"  Side        BUY {quantity} at limit {price}")
    print("  Product     I (intraday)")
    print(f"  Tag         {client_order_id}")
    print()
    print("  This is a REAL ORDER at a REAL EXCHANGE with REAL MONEY.")
    print("  It is a limit buy, so it can only fill at or below the price above.")
    print("  If that price is anywhere near the market, it WILL fill.")
    print()
    print(f'  Type exactly "{CONFIRMATION}" to continue, or anything else to stop.')
    if input("  > ").strip() != CONFIRMATION:
        print("  Stopped. Nothing was sent.")
        return "declined"

    print("\n  Placing...")
    try:
        order_ids = await client.place_order(
            instrument_token=instrument_key,
            quantity=quantity,
            product="I",
            order_type="LIMIT",
            transaction_type="BUY",
            price=price,
            tag=client_order_id,
        )
    except UpstoxApiError as exc:
        print(f"  [REFUSED] {exc}")
        print()
        print("  The three causes, in the order they are worth checking:")
        print("   1. The app is not an Algo Trading App, so it may not place orders.")
        print("   2. The outbound IP reported above is not registered against the app.")
        print("   3. Insufficient funds, or the instrument is not tradeable right now.")
        print()
        print("  The message above usually distinguishes them. If it does not,")
        print("  check them in that order — the first is the most common.")
        return "rejected"
    except UpstoxError as exc:
        print(f"  [UNKNOWN] {exc}")
        print()
        print("  The request may have reached the exchange. Check your order book")
        print("  in the Upstox app before running anything else.")
        return "unknown"

    print(f"  [OK] Accepted as {', '.join(order_ids)}.")
    print("  Placement is permitted. That was the question this script exists for.")

    print("\n  Re-reading the order book to look for our tag...")
    # Through the adapter, so this exercises the same normalisation live
    # recovery uses rather than a second reading of the same response.
    book = await UpstoxAdapter(client).normalised_orders()
    result = match_submission(book, client_order_id)
    if result.status == "RESOLVED_PLACED":
        print(f"  [OK] Found by tag as {', '.join(result.broker_order_numbers)}.")
        print("  Automatic recovery from a lost placement response will work.")
        verdict = "confirmed"
    else:
        print(f"  [PROBLEM] {result.detail}")
        print("  Recovery from a lost response would need manual intervention.")
        verdict = "tag-not-found"

    print("\n  Cancelling...")
    for order_id in order_ids:
        try:
            await client.cancel_order(order_id)
            print(f"  Cancellation requested for {order_id}.")
        except UpstoxError as exc:
            print(f"  [WARN] Could not cancel {order_id}: {exc}")
            print("  CANCEL IT BY HAND IN THE UPSTOX APP NOW.")
    print("  A cancellation request is not a cancellation. Confirm in the app.")
    return verdict


async def run(arguments: argparse.Namespace) -> int:
    # Imported here rather than at module scope: it pulls in the database
    # session, which builds Settings on import, so a top-level import would make
    # even --help fail on a machine without a configured environment. The first
    # thing anyone runs is --help.
    from app.services.upstox_oauth import load_access_token

    settings = get_settings()
    token = await load_access_token(settings)
    if not token:
        print("No Upstox access token is stored or configured.")
        print("Authorise the app first: Brokers -> Upstox -> Authorise in the web UI.")
        return 2

    client = UpstoxOrderClient(settings, UpstoxSession(access_token=token))
    findings = await stage_one(client, instrument_key=arguments.instrument_key, price=arguments.price)

    verdict = "not-run"
    if arguments.place_test_order:
        verdict = await stage_two(
            client,
            instrument_key=arguments.instrument_key,
            price=arguments.price,
            quantity=arguments.quantity,
        )

    heading("SUMMARY")
    address = findings.get("egress_ip")
    print(f"  Outbound IP:       {address or 'undetermined'} (must be registered with Upstox)")

    tag = findings.get("tag_supported")
    if tag is True:
        print("  tag round-trip:    CONFIRMED. UNKNOWN-order recovery is sound.")
    elif tag is False:
        print("  tag round-trip:    NOT SUPPORTED. Do not rely on automatic recovery.")
    else:
        print("  tag round-trip:    UNDETERMINED by reading alone. Re-run with --place-test-order.")

    print(f"  Margin readable:   {findings.get('margin', 'unverified')}")
    print(f"  Failure envelope:  {findings.get('envelope', 'unverified')}")
    print(f"  Placement:         {verdict}")
    if verdict == "not-run":
        print()
        print("  Placement was NOT tested. Reading the API proves nothing about")
        print("  whether this app may place an order — that is a separate")
        print("  permission, and only a placement settles it.")
    return 0 if verdict in {"confirmed", "not-run", "declined"} else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--place-test-order",
        action="store_true",
        help="Place and cancel ONE REAL ORDER to prove placement is permitted.",
    )
    parser.add_argument(
        "--instrument-key",
        default="",
        help='Upstox instrument key, e.g. "NSE_EQ|INE669E01016". Required for stage 2.',
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0.0,
        help="Limit price. Use one far BELOW the market so the buy cannot fill.",
    )
    parser.add_argument("--quantity", type=int, default=1, help="Shares. Leave at 1.")
    arguments = parser.parse_args()

    if arguments.place_test_order and not (arguments.instrument_key and arguments.price > 0):
        parser.error("--place-test-order requires --instrument-key and --price; neither has a safe default.")

    sys.exit(asyncio.run(run(arguments)))


if __name__ == "__main__":
    main()
