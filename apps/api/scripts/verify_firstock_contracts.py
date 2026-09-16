"""Check what Firstock's API actually returns, against what this system assumes.

Run this once with live credentials before trading real money. Every safeguard
in the live-execution layer was built from a reference document rather than from
observed responses, and three of those assumptions decide whether a lost order
can be found again:

  1. The order book returns the ``remarks`` field we send with a placement.
     Recovery from an UNKNOWN submission depends entirely on it. If it does not
     come back, an order whose response we lost cannot be identified except by
     guessing from symbol and quantity — which cannot tell our order apart from
     a second one like it. This is the assumption to verify first.

  2. The failure envelope carries status/code/name/error as documented, so the
     client can tell a refusal from an authentication fault from a rate limit.

  3. The position book reports netQuantity in a form we can parse, since an
     unparseable one blocks trading by design.

The script runs in two stages, and the difference matters:

    STAGE 1 (default)   Reads only. Cannot create, modify or cancel anything.
                        It answers question 1 immediately *if* your account
                        already has orders in today's book — from this system or
                        from manual trading. With an empty book it can only
                        report that it could not tell.

    STAGE 2 (opt-in)    Places one real order to settle question 1 definitively,
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

    docker compose exec api python scripts/verify_firstock_contracts.py
    docker compose exec api python scripts/verify_firstock_contracts.py \
        --place-test-order --symbol IDEA-EQ --price 1.00
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

from app.core.config import get_settings  # noqa: E402
from app.services.firstock.client import FirstockClient, FirstockError  # noqa: E402
from app.services.firstock.orders import FirstockOrderClient, cancellation_confirmed  # noqa: E402
from app.services.live_order_recovery import REMARKS_KEYS, match_submission  # noqa: E402
from app.services.live_orders import new_client_order_id  # noqa: E402

CONFIRMATION = "PLACE A REAL ORDER"


def heading(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def field_names(records: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for record in records:
        if isinstance(record, dict):
            names.update(str(key) for key in record)
    return sorted(names)


def report_remarks_support(order_book: list[dict[str, Any]]) -> bool | None:
    """True, False, or None for "the book was empty so we could not tell"."""
    if not order_book:
        print("  The order book is empty, so this cannot be answered by reading alone.")
        print("  Either run this again on a day with orders, or use stage 2.")
        return None

    present = [key for key in REMARKS_KEYS if any(key in record for record in order_book)]
    print(f"  Fields present on order records: {', '.join(field_names(order_book))}")
    if present:
        print(f"  [OK] The order book carries {', '.join(present)}. Automatic recovery works.")
        return True
    print("  [PROBLEM] No remarks-like field came back.")
    print("  An order whose placement response is lost could not be identified automatically.")
    print("  Ask Firstock which field echoes the remarks sent with placeOrder.")
    return False


async def stage_one(client: FirstockOrderClient) -> dict[str, Any]:
    findings: dict[str, Any] = {}

    heading("1. Does the order book echo the remarks we send?")
    order_book = await client.order_book()
    print(f"  {len(order_book)} order(s) in today's book.")
    findings["remarks_supported"] = report_remarks_support(order_book)

    heading("2. Position book: is netQuantity parseable?")
    positions = await client.position_book()
    if not positions:
        print("  No open positions, so nothing to parse. Re-run while holding one.")
    else:
        print(f"  Fields present: {', '.join(field_names(positions))}")
        for position in positions:
            symbol = position.get("tradingSymbol", "?")
            raw = position.get("netQuantity")
            print(f"  {symbol}: netQuantity={raw!r} (type {type(raw).__name__})")
    findings["positions"] = len(positions)

    heading("3. Account limits")
    try:
        limits = await client.limits()
        print(f"  Fields present: {', '.join(sorted(str(key) for key in limits))}")
        for key in ("cash", "payin", "marginUsed", "availableMargin"):
            if key in limits:
                print(f"  {key}: {limits[key]!r}")
    except FirstockError as exc:
        print(f"  [WARN] limits failed: {exc}")

    heading("4. Failure envelope: does a bad request classify as documented?")
    try:
        await client.single_order_history("definitely-not-an-order-number")
        print("  [WARN] A nonsense order number did not produce an error.")
        print("  The client's refusal handling could not be exercised.")
        findings["envelope"] = "unverified"
    except FirstockError as exc:
        print(f"  [OK] Refused as {type(exc).__name__}: {exc}")
        findings["envelope"] = type(exc).__name__

    return findings


async def stage_two(client: FirstockOrderClient, *, symbol: str, price: str, exchange: str, product: str) -> None:
    client_order_id = new_client_order_id()

    heading("STAGE 2: placing one real order")
    print(f"  Symbol      {symbol} on {exchange}")
    print(f"  Side        BUY, quantity 1, LIMIT at {price}")
    print(f"  Product     {product}")
    print(f"  remarks     {client_order_id}")
    print("\n  This is a real order at a real exchange with real money.")
    print("  It is a BUY limit, so it cannot fill while the market trades above your price.")
    print("  Verify that price is well below the market before continuing.\n")
    answer = input(f'  Type "{CONFIRMATION}" to continue, anything else to abort: ')
    if answer.strip() != CONFIRMATION:
        print("  Aborted. Nothing was sent.")
        return

    order_numbers: list[str] = []
    try:
        print("\n  Placing...")
        data = await client.place_order(
            exchange=exchange,
            trading_symbol=symbol,
            product=product,
            price_type="LMT",
            transaction_type="B",
            retention="DAY",
            quantity="1",
            price=price,
            trigger_price="0",
            remarks=client_order_id,
        )
        print(f"  Response: {data!r}")

        print("\n  Re-reading the order book to look for our remarks...")
        book = await client.order_book()
        result = match_submission(book, client_order_id)
        order_numbers = result.broker_order_numbers
        if result.status == "RESOLVED_PLACED":
            print(f"  [OK] Found by remarks as {', '.join(order_numbers)}.")
            print("  Automatic recovery from a lost placement response will work.")
        else:
            print(f"  [PROBLEM] {result.detail}")
            print("  Recovery from a lost response would need manual intervention.")
            # Fall back to the response's own number so cancellation can proceed.
            if isinstance(data, dict) and data.get("orderNumber"):
                order_numbers = [str(data["orderNumber"])]
    except FirstockError as exc:
        print(f"  Placement failed: {exc}")
        print("  If this was a timeout the order may still exist. Check your order book.")
    finally:
        if not order_numbers:
            print("\n  No order number to cancel. CHECK YOUR ORDER BOOK MANUALLY.")
        for number in order_numbers:
            print(f"\n  Cancelling {number}...")
            try:
                response = await client.cancel_order(number)
                confirmed, detail = cancellation_confirmed(response)
                print(f"  {'[OK]' if confirmed else '[WARN]'} {detail}")
            except FirstockError as exc:
                print(f"  [WARN] Cancellation failed: {exc}")
                print("  CANCEL THIS ORDER MANUALLY BEFORE LEAVING.")

        print("\n  Confirming terminal state from the order book...")
        try:
            for record in await client.order_book():
                if str(record.get("orderNumber")) in order_numbers:
                    print(f"  {record.get('orderNumber')}: status={record.get('status')!r}")
        except FirstockError as exc:
            print(f"  [WARN] Could not confirm: {exc}. Check your order book.")


async def run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.firstock_is_configured:
        print("Firstock credentials are not configured. Set them in .env first.")
        return 2

    print("Logging in to Firstock...")
    try:
        session = await FirstockClient(settings).login()
    except FirstockError as exc:
        print(f"Login failed: {exc}")
        return 2
    print("Logged in.")

    client = FirstockOrderClient(settings, session)
    findings = await stage_one(client)

    if arguments.place_test_order:
        await stage_two(
            client,
            symbol=arguments.symbol,
            price=arguments.price,
            exchange=arguments.exchange,
            product=arguments.product,
        )

    heading("SUMMARY")
    supported = findings.get("remarks_supported")
    if supported is True:
        print("  remarks round-trip: CONFIRMED. UNKNOWN-order recovery is sound.")
    elif supported is False:
        print("  remarks round-trip: NOT SUPPORTED. Do not rely on automatic recovery.")
    else:
        print("  remarks round-trip: UNDETERMINED by reading alone. Re-run with --place-test-order.")
    print(f"  Failure envelope: {findings.get('envelope', 'unverified')}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--place-test-order",
        action="store_true",
        help="Place and cancel ONE REAL ORDER to settle the remarks question definitively.",
    )
    parser.add_argument("--symbol", default="", help="Firstock trading symbol, e.g. IDEA-EQ. Required for stage 2.")
    parser.add_argument("--price", default="", help="Limit price. Use one far below the market so it cannot fill.")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--product", default="C", help="C=cash and carry, I=intraday, M=margin.")
    arguments = parser.parse_args()

    if arguments.place_test_order and not (arguments.symbol and arguments.price):
        parser.error("--place-test-order requires --symbol and --price; neither has a safe default.")

    sys.exit(asyncio.run(run(arguments)))


if __name__ == "__main__":
    main()
