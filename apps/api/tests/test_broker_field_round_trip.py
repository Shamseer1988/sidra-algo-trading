"""A value the adapter produced must be usable to build the next order.

The defect, live, on a real fill:

    NOT PROTECTED: ... Unsupported order field: 'I'
    10 of NSE_EQ|INE238A01034 is open with nothing behind it.

An entry is described once -- canonical INTRADAY becomes Upstox's "I" -- and
``prepare_submission`` stores what the broker was asked for. The protection
path then built the stop from that stored value and described it a *second*
time, and ``_PRODUCT["I"]`` does not exist. The stop was refused, and so was
the market close that exists to cover for a refused stop, because both go
through the same translation.

Every test of that path passed, because every one of them used a fake adapter
whose ``describe`` returns resolved unconditionally. A fake that always says
yes cannot discover a value the real adapter rejects. So these use the real
adapters, and assert the property that actually matters: a field stored from a
description has to survive being fed back in.
"""

from decimal import Decimal

import pytest

from app.services.broker_adapter import (
    BUY,
    DELIVERY,
    INTRADAY,
    MARKET,
    BrokerOrder,
    FirstockAdapter,
    UpstoxAdapter,
)

ADAPTERS = {"upstox": UpstoxAdapter(object()), "firstock": FirstockAdapter(object(), None)}


def order(product: str, token: str = "NSE_EQ|INE238A01034") -> BrokerOrder:
    return BrokerOrder(
        instrument_token=token,
        side=BUY,
        quantity=10,
        order_type=MARKET,
        product=product,
        price=Decimal("0"),
        client_order_id="sidra-round-trip",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("product", [INTRADAY, DELIVERY])
async def test_upstox_rejects_its_own_translated_product(product: str) -> None:
    """The bug, stated as the property it broke.

    The adapter's own output is NOT valid input. That is not a fault in the
    adapter -- two vocabularies is the right design -- it is the reason callers
    must keep the canonical value rather than reusing the stored one.
    """
    adapter = ADAPTERS["upstox"]
    described = await adapter.describe(order(product))
    assert described.resolved

    # What prepare_submission writes to LiveOrderSubmission.product.
    again = await adapter.describe(order(described.product))
    assert not again.resolved, (
        f"{described.product!r} described twice without complaint. If the broker word and the canonical "
        "word ever coincide, this test stops protecting anything and the callers below are the only guard."
    )
    assert "Unsupported order field" in again.detail


@pytest.mark.asyncio
async def test_the_canonical_product_describes_every_time() -> None:
    """What the fix relies on: canonical is the value that round-trips."""
    for name, adapter in ADAPTERS.items():
        first = await adapter.describe(order(INTRADAY))
        second = await adapter.describe(order(INTRADAY))
        assert first.resolved and second.resolved, name
        assert first.product == second.product, name


def test_the_model_reads_the_canonical_product_back() -> None:
    """The property the exit paths now use, against the real mapped class."""
    from app.db.models import LiveOrderSubmission

    record = LiveOrderSubmission(
        client_order_id="sidra-x",
        broker="UPSTOX",
        exchange="NSE_EQ",
        trading_symbol="NSE_EQ|INE238A01034",
        # The broker's word, which is what this column has always held.
        product="I",
        price_type="MARKET",
        transaction_type="BUY",
        quantity=10,
        request_snapshot={"canonical": {"product": INTRADAY, "instrumentToken": "NSE_EQ|INE238A01034"}},
    )
    assert record.product == "I"
    assert record.canonical_product == INTRADAY
    assert LiveOrderSubmission(client_order_id="y", request_snapshot={}).canonical_product is None


def test_no_exit_path_reads_a_stored_broker_product_at_all() -> None:
    """The mechanical guard.

    The round-trip property above is only useful if nothing violates it, and
    these two modules are what build orders from submissions.

    Matched by pattern rather than by exact line. The first version of this
    test looked for ``product=submission.product`` -- the keyword form the bug
    happened to take -- and a mutation that reintroduced the bug as a plain
    assignment walked straight past it. A guard that only recognises the
    accident that already happened is not a guard.
    """
    import io
    import re
    import tokenize
    from pathlib import Path

    def without_comments(text: str) -> str:
        """Code only. Comments here explain the bug and name it, repeatedly."""
        kept = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        ]
        return " ".join(kept)

    # `.product` with a dot before it; `.canonical_product` cannot match, since
    # the character preceding `product` there is an underscore.
    stored_read = re.compile(r"\b(submission|record|stop|stops\s*\[\s*0\s*\]|entry)\s*\.\s*product\b")
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("live_protection.py", "live_exit_manager.py"):
        source = without_comments((root / name).read_text())
        hits = stored_read.findall(source)
        assert not hits, (
            f"{name} reads a stored broker product {hits}. That value was already translated once by "
            "describe(); translating it again is what refused a live stop and the close behind it. "
            "Use canonical_product."
        )
