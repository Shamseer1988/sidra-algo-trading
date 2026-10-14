"""The operator's share-price band, shared by the paper and live engines.

Narrow on purpose, like ``daily_limits``. The standing rule in this codebase is
that the live path shares no risk code with paper -- a bug in paper sizing that
merely writes a wrong journal entry becomes a wrong live order the moment the
path is shared. What is shared here is narrower than an engine: two numbers the
operator typed and a comparison against one price. It computes no P&L, reads no
ledger, and touches no database, and it stays shareable only while that is true.

It lives in its own module because both engines must enforce it and neither may
import the other. It began inside the paper risk engine -- the engine every
signal had to pass -- and that stopped being true the day the live path was
allowed to proceed past a paper refusal: a band enforced only in paper would
have let the live account buy exactly the shares the operator had excluded,
which is the ₹1,818 BHARTIARTL entry of 7 October all over again, and worse for
being real money.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - the settings route imports live modules back
    from app.api.routes.settings import TradingControls


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def outside_price_band(entry_price: object, controls: "TradingControls") -> str | None:
    """Why this share is outside the operator's price band, or None.

    The band existed only in ``universe``, which ranks the watchlist, and that
    module runs only when ``UNIVERSE_ENABLED`` is set -- off by default. So an
    operator who set a ₹1,500 cap on the Settings screen had set nothing: on
    7 October the system entered PAYTM at ₹1,745 and BHARTIARTL at ₹1,818. The
    universe filter also fails open when the day's list has not been built yet,
    and it ranks on previous-day candles, so a share that crossed the cap this
    morning would pass it even with everything switched on.

    So the band is enforced here instead, where a trade is actually decided: on
    the signal's own entry price, in the engine every signal must pass, whether
    or not the dynamic universe is running and whatever it decided. The refusal
    is recorded against the signal, so the operator reads "we found a setup and
    refused it" rather than seeing nothing at all.

    Only the saved control is used, and zero means unbounded. The environment
    values behind ``universe`` are deployment defaults for ranking a watchlist;
    a refusal to trade should come from the number the operator typed.

    The reason this matters is not neatness. A price cap is how a risk budget is
    kept spendable in whole shares: ₹100 of risk against a ₹1,818 share with a
    0.8% stop buys six, and against a ₹4,700 one buys one -- a position that
    spends the budget and earns a fraction of the target.
    """
    price = _decimal(entry_price)
    high = _decimal(getattr(controls, "universe_max_share_price", 0) or 0)
    low = _decimal(getattr(controls, "universe_min_share_price", 0) or 0)
    if high > 0 and price > high:
        return f"Share price {price} is above the {high} cap"
    if low > 0 and price < low:
        return f"Share price {price} is below the {low} floor"
    return None
