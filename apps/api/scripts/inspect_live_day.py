"""Everything that happened on the live path for one session, in one place.

Written for the morning after a trade behaved in a way the Telegram messages
did not explain. It answers four questions that currently need the broker's app
open beside the container logs:

  what did we send      price, trigger, quantity, and the words we sent them in
  what did they say     the broker's own status and its own refusal message
  what actually filled  quantity and average price, per leg
  what did it risk      planned against sent, and budget against the real stop
  what is a stop really the limit it rests on, which is not always the kind of
                        order we asked for, and when the exchange acted on it

Read-only everywhere. The broker side goes through the report client, which has
no method that can place, modify or cancel an order, so this is safe to run
against a live account during a session.

Usage:

    python scripts/inspect_live_day.py                 # today, IST
    python scripts/inspect_live_day.py --date 2026-10-07
    python scripts/inspect_live_day.py --no-broker     # local records only
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import LiveOrderSubmission, PaperSignal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.trade_counter import LIVE_PLACED_STATUSES as PLACED_STATUSES  # noqa: E402
from app.services.trade_counter import session_bounds_utc  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78
THIN = "-" * 78


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def today_ist() -> date:
    return datetime.now(MARKET_TIMEZONE).date()


def stale_book_note(session_date: date, *, today: date | None = None) -> str | None:
    """Why the broker's books are not the asked-for date's, when they are not.

    ``normalised_orders`` and ``normalised_positions`` take no date. They
    answer with whatever the account holds at the moment they are called, and
    Upstox's order book is intraday -- cleared between sessions -- so asked
    about a past date they answer about now. That answer was then printed
    under a heading naming the past date.

    An operator reading "LIVE SESSION 2026-10-15 ... POSITIONS: TATAPOWER"
    had no way to see that TATAPOWER was open *today*; everything above that
    line really was the 15th, read from our own records. Two sources, one
    heading, and the only difference between them was invisible.

    Returns None for today, where the two agree and a disclaimer would be
    noise on every ordinary run.
    """
    now = today or today_ist()
    if session_date >= now:
        return None
    return (
        f"The broker's books below are the account as it stands on {now}, NOT on {session_date}. "
        "They take no date: the order book is intraday and holds nothing from a past session, and "
        f"a position shown here is open now. Everything above this line is {session_date}'s, from "
        "our own records."
    )


def order_shape(record, sent_priceless: bool) -> str | None:  # noqa: ANN001
    """The limit the broker is holding an order at, when we did not set one.

    Every stop goes out as SL-M and every exit as MARKET, both without a
    price. Upstox answers with a limit anyway: on 9 October the stop rested at
    ₹1,236.10 against a ₹1,248.60 trigger, and the square-off -- sent as a
    plain MARKET order -- came back holding a limit of ₹1,249.80 against a
    ₹1,262.50 fill. Both are 1.00% bands Upstox computed, and both bound what
    the order can do:

      a stop that gaps more than 1% past its trigger does not fill, and the
      position stays open with the loss still running

      a square-off in a market falling more than 1% does not fill either, and
      an intraday position is left for the broker's own auto-square-off

    The number is read rather than reasoned about, and only where we sent no
    price -- a limit we chose ourselves is not a finding.
    """
    if not sent_priceless:
        return None
    limit = record.limit_price
    if limit is None:
        return "broker did not report a limit price"
    if limit <= 0:
        return "no limit - fills at whatever the book offers"
    trigger = record.trigger_price or Decimal("0")
    against = trigger if trigger > 0 else (record.average_price or Decimal("0"))
    if against <= 0:
        return f"held at a limit of {money(limit)}, which we did not set"
    if limit == against:
        return f"limit equals the trigger ({money(limit)}) - will not fill past it"
    band = abs(against - limit)
    return (
        f"limit {money(limit)} against {money(against)} - a {band / against * 100:.2f}% band "
        f"({money(band)}) that we did not set"
    )


def money(value) -> str:  # noqa: ANN001
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def ist(value: datetime | None) -> str:
    return value.astimezone(MARKET_TIMEZONE).strftime("%H:%M:%S") if value else "—"


# Order types we send without a price, in both vocabularies: MARKET and SL-M
# at Upstox, MKT and SL-MKT at Firstock.
PRICELESS = frozenset({"MARKET", "SL-M", "MKT", "SL-MKT"})


def canonical(row: LiveOrderSubmission, key: str) -> str:
    return str(((row.request_snapshot or {}).get("canonical") or {}).get(key) or "—")


async def report_charges(day: date, risked: list[Decimal], rr: Decimal | None) -> None:
    """What the day cost after the broker's own charges, and what that demands.

    This section was missing, and its absence was the most expensive gap in
    the report: without it the only P&L shown is gross, and the question every
    other number serves -- "is this configuration capable of making money" --
    cannot be asked. On a 100 risk budget the difference between 11 and 46 of
    charges is the difference between needing to win 45% of trades and 61%.

    The figures are the broker's, from ``broker_day_snapshots``. Two scheduled
    jobs write them: the 18:00 IST pass after the close, which usually gets the
    realised figure and no charges, and the 08:40 IST catch-up the next
    morning, which is when Upstox has actually settled them. A day fetched too
    early has a row with a null ``charges``, and that is reported as waiting
    rather than as zero -- a zero here would be a claim the broker never made.

    The empty case said "the 18:15 job writes it" and pointed at the backfill
    script, which was wrong twice over: there is no 18:15 job, and the backfill
    cannot help before the broker has settled the day. Running it on the
    evening of 14 October returned ten days and recorded nine of them,
    correctly leaving out the only day the operator was asking about.
    """
    from sqlalchemy import desc

    from app.db.models import BrokerDaySnapshot

    async with SessionLocal() as session:
        snapshots = list(
            (
                await session.scalars(
                    select(BrokerDaySnapshot)
                    .where(BrokerDaySnapshot.session_date == day)
                    .order_by(desc(BrokerDaySnapshot.fetched_at))
                )
            ).all()
        )

    print()
    print(RULE)
    print("CHARGES — the broker's own figures, not our estimates")
    print(RULE)

    if not snapshots:
        print("  Nothing fetched for this day.")
        if day >= datetime.now(MARKET_TIMEZONE).date():
            # Asking the broker now returns nothing, so sending an operator to
            # the backfill here wastes a command and reads as a failure.
            print("  Today's charges are not published until the broker settles the session.")
            print("  The 18:00 job takes the realised figure tonight and the 08:40 catch-up")
            print("  fills the charges tomorrow morning. Nothing to do.")
        else:
            print("  The 18:00 job writes it and the 08:40 catch-up fills the charges the next")
            print("  morning. If the day is older than that and this is still empty:")
            print("    docker compose exec api python scripts/backfill_broker_history.py")
        return

    settled = next((item for item in snapshots if item.charges is not None), None)
    newest = snapshots[0]
    if settled is None:
        print(f"  Fetched {len(snapshots)} time(s), most recently {newest.fetched_at.astimezone(MARKET_TIMEZONE)}.")
        print("  The broker has not published charges for this day yet -- the row says nothing,")
        print("  which is different from saying zero. Friday's catch-up pass fills it in.")
        if newest.realized_pnl is not None:
            print(f"  Their realised P&L so far: ₹{money(newest.realized_pnl)} (before charges)")
        return

    charges = settled.charges
    print(f"  source            : {settled.source}   fetched {settled.fetched_at.astimezone(MARKET_TIMEZONE)}")
    if settled.realized_pnl is not None:
        print(f"  realised (gross)  : ₹{money(settled.realized_pnl)}")
        print(f"  charges           : ₹{money(charges)}")
        print(f"  NET              : ₹{money(settled.realized_pnl - charges)}")
    else:
        print(f"  charges           : ₹{money(charges)}")
    if settled.turnover is not None:
        print(f"  turnover          : ₹{money(settled.turnover)}")
    if settled.trade_count:
        print(f"  trades            : {settled.trade_count}   ₹{money(charges / settled.trade_count)} each")

    # The whole point of the section. A fixed cost against a small risk budget
    # is what decides whether a reward:risk is viable at all, and it is not
    # visible anywhere else in this system.
    if not risked or not rr:
        return
    per_trade_charge = charges / len(risked)
    average_risk = sum(risked, start=Decimal("0")) / len(risked)
    if average_risk <= 0:
        return
    win = average_risk * rr
    required = (average_risk + per_trade_charge) / (win + average_risk) * 100
    print()
    print(f"  Charges are {per_trade_charge / average_risk * 100:.0f}% of the ₹{money(average_risk)} actually risked.")
    print(f"  At {rr}R that makes the break-even win rate {required:.1f}%:")
    print(
        f"    a win pays ₹{money(win - per_trade_charge)} net, a loss costs ₹{money(average_risk + per_trade_charge)}"
    )
    print("  Compare that with the win rate this strategy actually achieves before trusting it.")


async def report_live_caps(session_date: date, *, adapter) -> None:  # noqa: ANN001
    """The live account's own caps, and what this day has used against them.

    The three limits the live risk engine enforces -- the day's risk
    allocation, concurrent positions, open exposure -- plus the operator's
    share-price band. They used to live only in the paper risk engine, so an
    operator asking why a live day ended early was reading the wrong account's
    numbers. Printed from the gate's own arithmetic rather than recomputed.

    The allocation is this system's own record and is always readable; the
    other two come from the broker's book, so they are reported as missing
    rather than as zero when no adapter was handed in.
    """
    from app.services.live_usage import cap_lines, live_usage

    print()
    print(RULE)
    print(f"LIVE CAPS AND WHAT {session_date} HAS USED")
    print(RULE)
    # Half of this section is date-scoped and half is not. The allocation is
    # totalled from that date's own live submissions; the position and exposure
    # figures come from the broker's book, which is always now. On a past date
    # the two halves are different days and the heading named only one.
    stale = stale_book_note(session_date)
    if stale is not None and adapter is not None:
        print(f"  [NOTE] The allocation below is {session_date}'s, from our own records. The position and")
        print(f"         exposure figures are the broker's book as of {today_ist()}, because the book takes")
        print("         no date -- so on a past session they answer a question about now.")
        print()
    async with SessionLocal() as session:
        usage = await live_usage(session, adapter=adapter, session_date=session_date)
    hint = (
        "Re-run without --no-broker to have those read from the account."
        if adapter is None
        else "The gate reads these itself before every entry, and refuses if the book cannot be read."
    )
    for text in cap_lines(usage, missing_hint=hint):
        print(f"  {text}" if text else "")


async def main(args) -> int:
    settings = get_settings()
    session_date = args.date or datetime.now(MARKET_TIMEZONE).date()
    start, end = session_bounds_utc(session_date)

    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(LiveOrderSubmission)
                .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
                .order_by(LiveOrderSubmission.created_at.asc())
            )
        )
        signal_ids = {row.paper_signal_id for row in rows if row.paper_signal_id}
        signals = (
            {item.id: item for item in await session.scalars(select(PaperSignal).where(PaperSignal.id.in_(signal_ids)))}
            if signal_ids
            else {}
        )

    print(RULE)
    print(f"LIVE SESSION {session_date}   {len(rows)} submission(s)")
    print(RULE)
    if not rows:
        print("  Nothing was sent to a broker on this date.")
        return 0

    # --- what we sent, and what we were told ---------------------------------
    for row in rows:
        print()
        print(
            f"{ist(row.created_at)}  {row.trading_symbol}  {canonical(row, 'side')} "
            f"{canonical(row, 'orderType')}  qty {row.quantity}"
        )
        print(
            f"    we sent      price {money(row.price)}  trigger {money(row.trigger_price)}  "
            f"product {row.product}  broker words: {row.transaction_type}/{row.price_type}"
        )
        print(f"    our status   {row.status}   order id(s): {', '.join(row.broker_order_numbers or []) or '—'}")
        print(
            f"    broker says  status {row.broker_status or '—'}   filled {row.filled_quantity if row.filled_quantity is not None else '—'}"
            f"  at {money(row.average_fill_price)}"
        )
        if row.failure_message or row.failure_name or row.failure_code:
            # The column is named failure_message, and it holds whatever the
            # broker said -- "Accepted as 2610140000546650" included. Labelling
            # that REFUSAL printed the word on every successful order on
            # 14 October, which teaches an operator to skip the line that
            # exists to be read. The label follows the status instead.
            code = (
                f"[{row.failure_name or ''}{'/' if row.failure_name and row.failure_code else ''}"
                f"{row.failure_code or ''}] "
                if (row.failure_name or row.failure_code)
                else ""
            )
            label = "broker said" if row.status in PLACED_STATUSES else "REFUSAL    "
            print(f"    {label}  {code}{row.failure_message or ''}")
        if row.resolution_detail:
            print(f"    resolution   {row.resolution_detail}")

    # --- what the trade was supposed to risk, against what it did ------------
    print()
    print(RULE)
    print("SIZING — what the strategy planned against what reached the broker")
    print(RULE)
    entries = [row for row in rows if canonical(row, "orderType") in {"LIMIT", "MARKET"} and row.paper_signal_id]
    seen: set = set()
    # Carried out of this loop for the charges section below: what was really
    # risked per trade, and the reward:risk the signals were actually built to
    # -- taken from the trade's own target and stop rather than from a setting,
    # because a setting changed since is not what this day traded.
    risked: list[Decimal] = []
    reward_ratios: list[Decimal] = []
    for row in entries:
        signal = signals.get(row.paper_signal_id)
        if signal is None or signal.id in seen:
            continue
        seen.add(signal.id)
        entry = Decimal(str(signal.entry_price))
        stop = Decimal(str(signal.stop_price))
        planned_room = abs(stop - entry)
        sent_room = abs(stop - Decimal(str(row.price))) if row.price else None
        filled = row.average_fill_price
        real_room = abs(stop - Decimal(str(filled))) if filled else None
        budget = Decimal(str(signal.risk_amount))

        print()
        print(f"  {signal.instrument_token}  {signal.side}")
        print(f"    budget            ₹{money(budget)}")
        print(f"    signal entry      {money(entry)}   stop {money(stop)}   per share ₹{money(planned_room)}")
        print(f"    planned quantity  {signal.quantity}   -> risk ₹{money(planned_room * signal.quantity)}")
        if sent_room:
            print(f"    limit sent        {money(row.price)}   per share ₹{money(sent_room)}")
        print(
            f"    quantity sent     {row.quantity}"
            f"   -> worst case ₹{money(sent_room * row.quantity) if sent_room else '—'}"
        )
        if real_room:
            print(f"    filled at         {money(filled)}   per share ₹{money(real_room)}")
            print(
                f"    REAL risk         ₹{money(real_room * row.quantity)}"
                f"   ({(real_room * row.quantity / budget * 100):.0f}% of the budget)"
            )
            risked.append(real_room * row.quantity)
        target = getattr(signal, "target_price", None)
        if target and planned_room > 0:
            reward_ratios.append(abs(Decimal(str(target)) - entry) / planned_room)

    rr_used = (
        (sum(reward_ratios, start=Decimal("0")) / len(reward_ratios)).quantize(Decimal("0.01"))
        if reward_ratios
        else None
    )
    await report_charges(session_date, risked, rr_used)

    if args.no_broker:
        # Without the broker, the day's live allocation is still readable: it
        # is totalled from this system's own live rows, not from the account.
        # The other two caps need the book and are reported as missing.
        await report_live_caps(session_date, adapter=None)
        return 0

    # --- the broker's own books ---------------------------------------------
    from app.services.live_execution_gateway import live_report_adapter
    from app.services.upstox_orders import UpstoxError

    stale = stale_book_note(session_date)
    brokers = {row.broker for row in rows if row.broker} or {None}
    for broker in sorted(brokers, key=str):
        print()
        print(RULE)
        print(f"BROKER ORDER BOOK — {broker or 'default'}{'' if stale is None else f'  (as of {today_ist()})'}")
        print(RULE)
        if stale is not None:
            print(f"  [NOTE] {stale}")
            print()
        try:
            async with SessionLocal() as session:
                adapter = await live_report_adapter(settings, session, broker)
                book = await adapter.normalised_orders()
                positions = await adapter.normalised_positions()
        except (UpstoxError, Exception) as error:  # noqa: BLE001 - reporting tool
            print(f"  [PROBLEM] could not read {broker}: {type(error).__name__}: {error}")
            continue

        ours = {str(number) for row in rows for number in (row.broker_order_numbers or [])}
        # Which of ours went out carrying no price of their own, so a limit in
        # the book is the broker's and not something we chose. Both
        # vocabularies, because the column holds the broker's spelling while
        # the snapshot holds the canonical one.
        priceless = {
            str(number)
            for row in rows
            if canonical(row, "orderType") in PRICELESS or str(row.price_type or "").upper() in PRICELESS
            for number in (row.broker_order_numbers or [])
        }
        for record in book:
            mark = "*" if str(record.broker_order_id) in ours else " "
            print(
                f" {mark} {record.broker_order_id}  {record.symbol}  {record.side or '—'} "
                f"{record.order_type or '—'}  qty {record.quantity}  filled {record.filled_quantity}"
            )
            print(
                f"     status {record.status}  trigger {money(record.trigger_price)}  "
                f"limit {money(record.limit_price)}  avg {money(record.average_price)}"
            )
            # Not "placed". Upstox's order_timestamp moves on every change, so
            # on 9 October a stop sent at 09:41:02 reported 15:00:00 -- the
            # moment it was cancelled. Labelling that as the placement time
            # made a stop that rested all day look like one placed at the
            # close. When it was sent is our own record, printed above.
            print(
                f"     last update {record.placed_at or '—'}  exchange {record.exchange_at or '—'}  "
                f"tag {record.client_order_id or '—'}"
            )
            if record.status_message:
                # The field that would have said "invalid order price" in a
                # Telegram message instead of in the broker's app.
                print(f"     BROKER MESSAGE: {record.status_message}")
            note = order_shape(record, str(record.broker_order_id) in priceless)
            if note:
                print(f"     BROKER'S OWN LIMIT: {note}")
        if not book:
            print("  (the order book is empty)")

        print()
        print(THIN)
        print("POSITIONS" if stale is None else f"POSITIONS — current broker book, not {session_date}")
        print(THIN)
        for record in positions:
            print(
                f"  {record.symbol}  net {record.net_quantity}  realised {money(record.realised)}  "
                f"unrealised {money(record.unrealised)}  day {money(record.day_pnl)}"
            )
        if not positions:
            print("  (flat)")

        # The caps the live risk engine will apply to the next entry, against
        # what this account has already used. Read with the same adapter the
        # positions above came from, so the two cannot disagree.
        await report_live_caps(session_date, adapter=adapter)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    parser.add_argument("--no-broker", action="store_true", help="Local records only; ask the broker nothing.")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
