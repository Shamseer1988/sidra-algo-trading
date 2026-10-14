"""What the live account has used today, against what it is allowed.

The live risk engine gained its own position, exposure and daily-allocation
caps, read from the live account rather than from the paper ledger. That closed
a real hole and opened a smaller one: the caps now bind on numbers an operator
cannot see anywhere. On 14 October the live day ended after one trade and the
answer took a transcript of logs to find; the next time a day ends early the
answer should be one command.

So this reports the same numbers the gate decides on, and reports them by
calling the gate's own arithmetic rather than repeating it. Two
implementations of one ceiling drift, and the one an operator reads would be
the one enforcing nothing.

Read-only. It reads the saved controls, today's live submissions, and -- only
when handed an adapter -- the broker's position book. Handed none, the book
half is reported as not asked rather than as empty: an unknown position count
and a position count of zero are different answers, and the gate itself
refuses on the first.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.broker_adapter import BrokerAdapter
from app.services.live_risk import (
    allocation_budget,
    exposure_ceiling,
    live_controls,
    live_exposure,
    live_risk_allocated_today,
    open_positions,
)

# Three states, not two. "I did not ask" and "I asked and could not tell" look
# alike in a dataclass and mean opposite things to an operator: the first is a
# gap in this report, the second is a gate currently refusing every live order.
# Collapsing them is how a report ends up crying wolf on every run, which is
# worse than not having one.
KNOWN = "known"
NOT_ASKED = "not_asked"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Cap:
    """One limit, what has been used against it, and whether it now binds.

    ``binding`` is what the operator wants: not "is this configured" but
    "would this refuse the next live entry right now". A figure the broker was
    never asked for is not binding and is not clear either -- it is missing,
    and saying "0 of 3" would be a reassuring lie while claiming it blocks
    would be a false alarm on every run without a broker.
    """

    key: str
    label: str
    used: Decimal | None
    limit: Decimal
    detail: str
    state: str = KNOWN
    # The gate refuses a *count* that has reached the limit and an *amount*
    # only once it is above the ceiling. One shared comparison would make the
    # report disagree with the gate by exactly one position or one rupee, in
    # the direction that reads as room there is not.
    at_limit_binds: bool = True

    @property
    def known(self) -> bool:
        return self.state == KNOWN and self.used is not None

    @property
    def configured(self) -> bool:
        return self.limit > 0

    @property
    def binding(self) -> bool:
        # Unreadable first, and before the limit is consulted. The gate refuses
        # when it asked and could not establish the figure, including on an
        # unreadable controls row -- where the limit itself reads as zero, so a
        # "configured" test taken first would report nothing blocking while
        # every live order was being turned away.
        if self.state == UNREADABLE:
            return True
        if not self.known:
            return False
        if not self.configured:
            return False
        return self.used >= self.limit if self.at_limit_binds else self.used > self.limit

    @property
    def headroom(self) -> Decimal | None:
        if not self.configured or not self.known:
            return None
        return self.limit - self.used


@dataclass(frozen=True)
class LiveUsage:
    session_date: date
    controls_readable: bool
    allocation: Cap
    trades: int
    positions: Cap
    exposure: Cap
    price_band: str
    symbols: tuple[str, ...] = ()

    @property
    def caps(self) -> tuple[Cap, ...]:
        return (self.allocation, self.positions, self.exposure)

    @property
    def blocking(self) -> tuple[Cap, ...]:
        """Every cap that would refuse the next live entry, in report order."""
        return tuple(cap for cap in self.caps if cap.binding)

    @property
    def not_asked(self) -> tuple[Cap, ...]:
        """Caps this run could not report, as distinct from caps that are full."""
        return tuple(cap for cap in self.caps if cap.state == NOT_ASKED)


_UNREADABLE = "The saved trading controls could not be read."
_NOT_ASKED = "The broker was not asked; run this where a broker is selected to read the open book."


def _band(controls: Any) -> str:
    high = Decimal(str(getattr(controls, "universe_max_share_price", 0) or 0))
    low = Decimal(str(getattr(controls, "universe_min_share_price", 0) or 0))
    if high <= 0 and low <= 0:
        return "no band configured; every share price is allowed"
    if high > 0 and low > 0:
        return f"{low} to {high}, enforced on the live path as well as on paper"
    if high > 0:
        return f"up to {high}, enforced on the live path as well as on paper"
    return f"from {low} up, enforced on the live path as well as on paper"


def _book_caps(controls: Any, positions: Iterable[Any] | None, failure: str | None) -> tuple[Cap, Cap, tuple[str, ...]]:
    limit = Decimal(str(int(controls.maximum_open_positions or 0)))
    ceiling = exposure_ceiling(controls)
    if positions is None:
        # A broker that was asked and refused is a gate refusing live orders
        # right now; a broker that was never asked is only a gap in this run.
        state = UNREADABLE if failure else NOT_ASKED
        detail = failure or _NOT_ASKED
        return (
            Cap("positions", "Open live positions", None, limit, detail, state=state),
            Cap("exposure", "Open live exposure", None, ceiling, detail, state=state, at_limit_binds=False),
            (),
        )

    rows = list(positions)
    unreadable = sorted(item.symbol for item in rows if item.net_quantity is None)
    if unreadable:
        detail = (
            f"The broker reported an unreadable quantity for {', '.join(unreadable)}, "
            "so the open position count cannot be established. The gate refuses on this."
        )
        return (
            Cap("positions", "Open live positions", None, limit, detail, state=UNREADABLE),
            Cap("exposure", "Open live exposure", None, ceiling, detail, state=UNREADABLE, at_limit_binds=False),
            tuple(unreadable),
        )

    held = open_positions(rows)
    symbols = tuple(sorted(item.symbol for item in held))
    exposure = live_exposure(held)
    return (
        Cap(
            "positions",
            "Open live positions",
            Decimal(len(held)),
            limit,
            ", ".join(symbols) if symbols else "the live book is flat",
        ),
        Cap(
            "exposure",
            "Open live exposure",
            exposure,
            ceiling,
            "from the broker's own position book",
            at_limit_binds=False,
        ),
        symbols,
    )


async def live_usage(
    session: AsyncSession,
    *,
    adapter: BrokerAdapter | None = None,
    session_date: date | None = None,
) -> LiveUsage:
    """Today's live caps and today's live usage, in one object.

    The adapter is optional so this is usable from a script that has no broker
    selected, or during a session when an operator would rather not add a
    broker call. What it cannot do is pretend: without an adapter the position
    and exposure caps come back with state NOT_ASKED -- missing, not clear,
    and not a false alarm either.
    """
    day = session_date or datetime.now(UTC).date()
    # The gate's own loader, so the report is about the controls the gate
    # loads -- including its refusal to fall back to defaults the operator
    # never chose.
    controls = await live_controls(session)
    if controls is None:
        zero = Decimal("0")
        return LiveUsage(
            session_date=day,
            controls_readable=False,
            allocation=Cap("allocation", "Live risk allocated today", None, zero, _UNREADABLE, state=UNREADABLE),
            trades=0,
            positions=Cap("positions", "Open live positions", None, zero, _UNREADABLE, state=UNREADABLE),
            exposure=Cap("exposure", "Open live exposure", None, zero, _UNREADABLE, state=UNREADABLE),
            price_band=_UNREADABLE,
        )

    allocated, trades = await live_risk_allocated_today(session, day)
    positions: list[Any] | None = None
    failure: str | None = None
    if adapter is not None:
        try:
            positions = list(await adapter.normalised_positions())
        except Exception as exc:  # noqa: BLE001 - a report never raises at an operator
            failure = f"The broker position book could not be read: {exc}. The gate refuses on this."

    position_cap, exposure_cap, symbols = _book_caps(controls, positions, failure)
    return LiveUsage(
        session_date=day,
        controls_readable=True,
        allocation=Cap(
            "allocation",
            "Live risk allocated today",
            allocated,
            allocation_budget(controls),
            f"across {trades} live trade(s), from the risk each live order was sized to take",
        ),
        trades=trades,
        positions=position_cap,
        exposure=exposure_cap,
        price_band=_band(controls),
        symbols=symbols,
    )


# What to do about the caps this run could not read. The caller supplies it
# because the answer depends on which command is being run: a script that never
# calls the broker points at the one that does, and the one that does -- asked
# not to -- points at its own flag. A single default would have one of the two
# telling an operator to run the command they are already running.
DEFAULT_MISSING_HINT = (
    "Run inspect_live_day.py for those; the gate asks the broker itself and\nrefuses if the book cannot be read."
)


def cap_lines(usage: LiveUsage, *, missing_hint: str = DEFAULT_MISSING_HINT) -> list[str]:
    """The report as lines, for whichever script is printing it.

    Here rather than in a script because two scripts formatting one set of caps
    is two readings of them, and the operator would have no way to know which
    one matched the gate.
    """
    if not usage.controls_readable:
        return [
            "[BAD ] The saved trading controls could not be read.",
            "       Every live order is being refused on this; fix the settings row first.",
        ]

    lines: list[str] = []
    for cap in usage.caps:
        if not cap.known:
            # "Full" and "I could not tell" are marked differently on purpose:
            # a report that flags the second as the first cries wolf on every
            # run made without a broker, and gets ignored by the third.
            lines.append(f"[{'BAD ' if cap.binding else ' .. '}] {cap.label:<28} not established")
            lines.append(f"       {cap.detail}")
            continue
        if not cap.configured:
            lines.append(f"[OK  ] {cap.label:<28} no limit configured")
            continue
        headroom = cap.headroom
        room = f"  room {headroom:,.2f}" if headroom is not None and headroom > 0 else ""
        lines.append(f"[{'FULL' if cap.binding else 'OK  '}] {cap.label:<28} {cap.used:,.2f} of {cap.limit:,.2f}{room}")
        lines.append(f"       {cap.detail}")

    lines.append(f"[OK  ] {'Share price band':<28} {usage.price_band}")
    blocking = [cap.label for cap in usage.blocking]
    missing = [cap.label for cap in usage.not_asked]
    lines.append("")
    if blocking:
        lines.append("Blocking the next live entry right now: " + ", ".join(blocking))
    elif missing:
        lines.append("Nothing that could be read here would refuse the next live entry.")
    else:
        lines.append("Nothing here would refuse the next live entry.")
    if missing:
        lines.append("Not read here, because the broker was not asked: " + ", ".join(missing) + ".")
        lines.extend(missing_hint.splitlines())
    return lines
