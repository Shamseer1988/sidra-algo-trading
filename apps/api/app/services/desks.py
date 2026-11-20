"""Which book a trade belongs to: the intraday desk or the swing desk.

The account runs, or will run, two kinds of trading with different rules. An
intraday trade is flat by the square-off; a swing trade is meant to be held
overnight for days. The two must never be confused, and today nothing in the
order path can tell them apart:

* **The product does not say.** ``live_shadow.product_for`` picks INTRADAY when
  ``intraday_leverage_enabled`` is on and DELIVERY when it is off. So an
  intraday trade placed with leverage off is already a DELIVERY order, and a
  swing trade would be one too. Reading the desk back from the product would
  call that intraday trade a swing position, or the reverse.
* **The day does not say.** A swing entry filled this morning is in today's
  position book next to the intraday trades, with a signal from today behind
  it.

So the desk is recorded explicitly, on the signal and on every live order, at
the moment they are written. This module is the one place that says what a
desk is and what follows from it. It is pure: no database, no broker, nothing
that can place or change an order.

**Intraday behaviour is defined as exactly what it was.** ``product_for_desk``
returns, for the intraday desk, what ``product_for`` returns -- leverage flag
and all. That is pinned by a test, so wiring this in cannot quietly change the
product of a live intraday order. Every row written before the desk existed is
intraday, and the migration says so with a server default.

**Unknown means stop, not intraday.** Code elsewhere falls back to INTRADAY
when it cannot read a product. For an intraday position that guess is right,
which is why it was written. For a swing holding it is the worst available
answer: selling delivery shares as an intraday order is either refused by the
broker or opens an intraday short. ``exit_product`` keeps the old fallback for
the intraday desk only and refuses for any other desk.
"""

from dataclasses import dataclass

from app.services.broker_adapter import DELIVERY, INTRADAY

INTRADAY_DESK = "INTRADAY"
SWING_DESK = "SWING"
DESKS = (INTRADAY_DESK, SWING_DESK)

# A swing position is held overnight in the cash segment, which cannot carry a
# short past the close. Long only is a property of the desk, not a setting.
SWING_SIDES = ("LONG",)


class UnknownDeskError(ValueError):
    """A desk name this system does not have."""


class ExitProductUnknownError(RuntimeError):
    """An exit cannot be sent because nothing records the position's product."""


def normalise_desk(value: str | None) -> str:
    """The desk in canonical form. Missing means intraday; anything else unknown raises.

    Missing is intraday because every row and every caller that predates the
    desk is intraday. A misspelt desk is not given the same treatment: a typo in
    a swing configuration landing on the intraday desk would put it under the
    15:00 square-off.
    """
    if value is None or str(value).strip() == "":
        return INTRADAY_DESK
    desk = str(value).strip().upper()
    if desk not in DESKS:
        raise UnknownDeskError(f"Unknown desk {value!r}; expected one of {', '.join(DESKS)}")
    return desk


def product_for_desk(desk: str | None, *, intraday_leverage_enabled: bool) -> str:
    """The product an entry on this desk is placed with.

    Intraday: unchanged from ``live_shadow.product_for`` -- INTRADAY with
    leverage, DELIVERY without. Swing: DELIVERY always, and the leverage flag is
    not read, so changing it can never turn a swing entry into an intraday one.
    """
    desk = normalise_desk(desk)
    if desk == SWING_DESK:
        return DELIVERY
    return INTRADAY if intraday_leverage_enabled else DELIVERY


def side_allowed(desk: str | None, side: str) -> bool:
    """Whether a signal on this side may be traded on this desk."""
    desk = normalise_desk(desk)
    side = (side or "").strip().upper()
    if desk == SWING_DESK:
        return side in SWING_SIDES
    return side in ("LONG", "SHORT")


def squared_off_by_session_close(desk: str | None) -> bool:
    """Whether this desk's positions are closed by the day's square-off."""
    return normalise_desk(desk) == INTRADAY_DESK


def counts_toward_daily_ceilings(desk: str | None) -> bool:
    """Whether a fill on this desk uses up the intraday trades-per-day ceilings.

    The swing desk is bounded by open positions and open risk instead. Letting a
    swing entry spend the intraday desk's three trades a day would stop the
    intraday desk for a reason that has nothing to do with it.
    """
    return normalise_desk(desk) == INTRADAY_DESK


@dataclass(frozen=True)
class ExitProduct:
    product: str
    recorded: bool  # False when the intraday fallback supplied it


def exit_product(desk: str | None, recorded_product: str | None) -> ExitProduct:
    """The product to close a position with.

    The product recorded on the order that opened it, when there is one. When
    there is not: INTRADAY for the intraday desk, as the exit manager has always
    done, and a refusal for every other desk.
    """
    desk = normalise_desk(desk)
    if recorded_product:
        return ExitProduct(product=recorded_product, recorded=True)
    if desk == INTRADAY_DESK:
        return ExitProduct(product=INTRADAY, recorded=False)
    raise ExitProductUnknownError(
        f"No product is recorded for this {desk.lower()} position, so no exit was sent. "
        "Guessing INTRADAY could open a short against delivery shares. Close it by hand and check the order record."
    )


# Which desk each on/off switch belongs to, and what it is when nothing is
# saved: exactly what every deployment did before the desks existed.
DESK_SWITCHES = {INTRADAY_DESK: ("intraday_desk_enabled", True), SWING_DESK: ("swing_desk_enabled", False)}


def desk_enabled(controls, desk: str | None) -> bool:  # noqa: ANN001 - a dict or a TradingControls
    """Whether this desk may open new trades.

    Only new entries ask. Exits, stops and the square-off never do: a desk
    switched off with a position open must still be able to close it.
    """
    key, default = DESK_SWITCHES[normalise_desk(desk)]
    value = controls.get(key, default) if isinstance(controls, dict) else getattr(controls, key, default)
    return bool(value)
