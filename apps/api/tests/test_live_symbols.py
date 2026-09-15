"""Instrument token to Firstock order symbol.

The failure this file is written against is not a rejected order. It is an
accepted one, for a different instrument than the scanner analysed. So the
refusals are the tests that matter: every input we cannot defend must produce
``resolved is False`` rather than a plausible-looking string.
"""

import pytest

from app.services.live_symbols import SymbolTranslation, translate_for_order


class FakeSession:
    """Stands in for the instrument-master lookup, which is one scalar query."""

    def __init__(self, master: dict[str, dict] | None = None) -> None:
        self._master = master

    async def scalar(self, _query: object) -> object | None:
        if self._master is None:
            return None
        return type("Refresh", (), {"configured_keys": self._master})()


async def translate(token: str, master: dict[str, dict] | None = None) -> SymbolTranslation:
    return await translate_for_order(FakeSession(master), token)


async def test_known_equity_isin_resolves_to_a_series_qualified_symbol() -> None:
    result = await translate("NSE_EQ|INE002A01018")
    assert result.resolved is True
    assert result.trading_symbol == "RELIANCE-EQ"
    assert result.exchange == "NSE"
    assert result.status == "RESOLVED"


async def test_known_numeric_token_resolves() -> None:
    assert (await translate("NSE:2885")).trading_symbol == "RELIANCE-EQ"


async def test_instrument_master_resolves_what_the_static_map_does_not() -> None:
    result = await translate(
        "NSE_EQ|INE669E01016",
        {"NSE_EQ|INE669E01016": {"trading_symbol": "IDEA"}},
    )
    assert result.resolved is True
    assert result.trading_symbol == "IDEA-EQ"
    assert "instrument master" in result.reason


async def test_a_master_symbol_that_already_carries_the_series_is_not_doubled() -> None:
    result = await translate("NSE_EQ|INE669E01016", {"NSE_EQ|INE669E01016": {"trading_symbol": "IDEA-EQ"}})
    assert result.trading_symbol == "IDEA-EQ"


@pytest.mark.parametrize(
    "token",
    ["NSE_INDEX|Nifty 50", "NSE_INDEX|NIFTY BANK", "NSE:26000", "NSE:26001", "NSE:26009"],
)
async def test_indices_refuse_because_they_cannot_be_bought_as_shares(token: str) -> None:
    """The scanner holds a NIFTY token as its relative-strength benchmark."""
    result = await translate(token)
    assert result.resolved is False
    assert result.trading_symbol is None
    assert "index" in result.reason.lower() or "exchange" in result.reason.lower()


async def test_an_index_arriving_through_the_instrument_master_still_refuses() -> None:
    """The index guard checks the resolved name, not the shape of the token."""
    result = await translate("NSE_EQ|INE999X01011", {"NSE_EQ|INE999X01011": {"trading_symbol": "FINNIFTY"}})
    assert result.resolved is False


async def test_an_unmapped_token_refuses_instead_of_guessing() -> None:
    """resolve_script_name would hand back the token itself. That must not happen here."""
    result = await translate("NSE_EQ|INE123Z01099")
    assert result.resolved is False
    assert result.trading_symbol is None
    assert "verified mapping" in result.reason


async def test_a_symbol_shaped_token_is_not_accepted_on_its_appearance() -> None:
    """NSE:SOMETHING looks resolvable and is not; only verified sources count."""
    result = await translate("NSE:SOMECO")
    assert result.resolved is False


@pytest.mark.parametrize("token", ["", "   ", "BSE_EQ|INE002A01018", "NFO|BANKNIFTY24SEP", "garbage"])
async def test_unsupported_or_empty_tokens_refuse(token: str) -> None:
    result = await translate(token)
    assert result.resolved is False
    assert result.exchange is None


async def test_a_refusal_never_carries_a_symbol_a_caller_could_use() -> None:
    """Callers read trading_symbol; a refusal that still filled it would be lethal."""
    for token in ["", "NSE:26000", "NSE_EQ|INE123Z01099", "NFO|X"]:
        result = await translate(token)
        assert result.resolved is False
        assert result.trading_symbol is None
        assert result.status == "UNRESOLVED"
