"""A subscribed key the broker does not recognise, named with its key.

The watchlist printed "tick unknown" on the row and left it at that. The 14
October snapshot said 28 subscribed and 27 with a tick size, so one instrument
was unidentified -- and finding out which meant reading 28 rows, then re-running
with --tokens to get the key, because the key is the only thing that can be
removed from UPSTOX_SUBSCRIPTIONS.

Two findings are separated here because their consequences differ. A key
missing from the broker's instrument master has no symbol either, so it cannot
be named in an order and the live gate's instrument check refuses every signal
on it: that is a definite loss. A key with a symbol but no tick falls back to
the default grid, which costs a refused order on a coarse-grid share: a
possible loss.
"""

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY
from app.db.models import ApplicationSetting, InstrumentMasterRefresh, MarketCandle
from app.db.session import SessionLocal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import watchlist  # noqa: E402

# ISIN form, which is what a real Upstox equity key looks like. It matters:
# resolve_script_name returns the bare suffix for a symbol-form key, so
# "NSE_EQ|SBIN" is named whether or not the broker knows it, while an ISIN key
# falls back to the key. Membership in the master is therefore what decides,
# and both spellings are covered below.
NAMED = "NSE_EQ|INE900W01011"
TICKLESS = "NSE_EQ|INE900W01022"
UNKNOWN = "NSE_EQ|INE900W01033"
SYMBOL_FORM = "NSE_EQ|WL-NOTREAL"
TOKENS = (NAMED, TICKLESS, UNKNOWN, SYMBOL_FORM)
MARKER = "https://example.invalid/watchlist-test-master.json"
BENCHMARK = "NSE_INDEX|Nifty 50"


def settings(subscriptions: str, *, universe_enabled: bool = False):
    """Only the fields the script reads, so a stub answers for no more."""
    return type(
        "Settings",
        (),
        {
            "upstox_subscriptions": subscriptions,
            "upstox_nifty_benchmark_key": BENCHMARK,
            "upstox_india_vix_key": "",
            "universe_enabled": universe_enabled,
            "market_regime_enabled": False,
        },
    )()


async def seed_master(entries: dict) -> None:
    async with SessionLocal() as session:
        session.add(
            InstrumentMasterRefresh(
                provider="UPSTOX",
                source_url=MARKER,
                fetched_at=datetime.now(UTC),
                payload_sha256="0" * 64,
                instrument_count=len(entries),
                configured_keys=entries,
                missing_keys=[],
            )
        )
        await session.commit()


async def seed_candle(token: str, close: str = "500") -> None:
    async with SessionLocal() as session:
        session.add(
            MarketCandle(
                instrument_token=token,
                timeframe_seconds=60,
                session_date=date(2026, 10, 14),
                opened_at=datetime(2026, 10, 14, 4, 0, tzinfo=UTC),
                closed_at=datetime(2026, 10, 14, 4, 1, tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=100,
            )
        )
        await session.commit()


async def save_controls(**overrides) -> None:
    async with SessionLocal() as session:
        row = await session.get(ApplicationSetting, TRADING_KEY)
        value = {**DEFAULT_TRADING_CONTROLS, **overrides}
        if row:
            row.value = value
        else:
            session.add(ApplicationSetting(key=TRADING_KEY, value=value))
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        # Scoped by source_url, the convention test_trading_symbols established.
        # Deleting every refresh row would reach outside this file, and a
        # shared database makes that somebody else's intermittent failure.
        await session.execute(delete(InstrumentMasterRefresh).where(InstrumentMasterRefresh.source_url == MARKER))
        await session.execute(
            delete(MarketCandle).where(MarketCandle.instrument_token.in_(TOKENS)),
        )
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, saved) -> str:  # noqa: ANN001
    """main() parses sys.argv itself, so the flags are set the way a shell would."""
    monkeypatch.setattr(watchlist, "get_settings", lambda: saved)
    monkeypatch.setattr(sys, "argv", ["watchlist.py"])
    assert await watchlist.main() == 0
    return capsys.readouterr().out


# --- a key the master does not have at all --------------------------------


async def test_an_unrecognised_key_is_named_with_its_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The key, because the key is what gets removed from the env file."""
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls()
    await seed_candle(NAMED)
    await seed_candle(UNKNOWN)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED},{UNKNOWN}"))
    assert "not in the broker's instrument master" in out
    assert UNKNOWN in out
    assert "Remove it from UPSTOX_SUBSCRIPTIONS" in out


async def test_a_recognised_key_is_not_flagged(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls()
    await seed_candle(NAMED)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}"))
    assert "not in the broker's instrument master" not in out
    assert "no tick size in the master" not in out


# --- a key with a symbol but no tick --------------------------------------


async def test_a_named_instrument_with_no_tick_is_a_warning_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """It can still be ordered; the cost is a price the exchange may refuse."""
    await seed_master(
        {
            NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5},
            TICKLESS: {"trading_symbol": "IDFCFIRSTB-EQ"},
        }
    )
    await save_controls()
    await seed_candle(NAMED)
    await seed_candle(TICKLESS)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED},{TICKLESS}"))
    assert "no tick size in the master" in out
    assert "IDFCFIRSTB-EQ" in out
    assert TICKLESS in out
    # Named, so it is not the stronger finding.
    assert "not in the broker's instrument master" not in out


async def test_an_unrecognised_key_is_reported_once_not_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """It has no tick either, and listing it under both reads as two problems."""
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls()
    await seed_candle(NAMED)
    await seed_candle(UNKNOWN)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED},{UNKNOWN}"))
    assert "not in the broker's instrument master" in out
    assert "no tick size in the master" not in out


# --- the ranking switch ---------------------------------------------------


async def test_the_saved_universe_switch_governs_what_this_says(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Reading .env here told an operator who had turned the ranking on that
    every instrument was scanned every candle."""
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls(universe_enabled=True)
    await seed_candle(NAMED)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}", universe_enabled=False))
    assert "universe_enabled is on" in out
    assert "is not running" not in out


async def test_the_saved_switch_being_off_is_reported_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls(universe_enabled=False)
    await seed_candle(NAMED)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}", universe_enabled=True))
    assert "universe_enabled is off" in out


async def test_a_symbol_form_key_the_broker_does_not_know_is_flagged_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """resolve_script_name names it from its own text, so it looks resolved.

    Deciding on the resolved name would have found the real deployment's ISIN
    keys and quietly missed this spelling, which is the one an operator is more
    likely to have typed by hand.
    """
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls()
    await seed_candle(NAMED)
    await seed_candle(SYMBOL_FORM)
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED},{SYMBOL_FORM}"))
    assert "not in the broker's instrument master" in out
    assert SYMBOL_FORM in out


# --- what an out-of-band name actually costs ------------------------------
#
# The advice said "scanned every candle ... drop them from
# UPSTOX_SUBSCRIPTIONS" whatever the ranking was doing. With the ranking on,
# the ranking applies the same band to the prior close and the scanner returns
# before any strategy runs for an instrument it did not select -- so the four
# names flagged on this deployment were costing nothing, and the advice was to
# fix nothing.


async def test_with_the_ranking_on_an_out_of_band_name_needs_no_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed_master({NAMED: {"trading_symbol": "HAL-EQ", "tick_size": 10}})
    await save_controls(universe_enabled=True, universe_max_share_price=2000.0)
    await seed_candle(NAMED, "4933")
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}"))
    assert "OUTSIDE THE BAND" in out
    assert "there is nothing to do about them" in out
    assert "drop them from" not in out
    # Not a warning, because nothing is wrong.
    assert "[NOTE]" in out


async def test_with_the_ranking_off_the_name_really_is_scanned_every_candle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """There the old advice was right, and it is kept."""
    await seed_master({NAMED: {"trading_symbol": "HAL-EQ", "tick_size": 10}})
    await save_controls(universe_enabled=False, universe_max_share_price=2000.0)
    await seed_candle(NAMED, "4933")
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}"))
    assert "scanned every candle" in out
    assert "UPSTOX_SUBSCRIPTIONS" in out
    assert "[WARN]" in out


async def test_a_name_inside_the_band_is_not_flagged_either_way(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed_master({NAMED: {"trading_symbol": "HDFCBANK-EQ", "tick_size": 5}})
    await save_controls(universe_enabled=True, universe_max_share_price=2000.0)
    await seed_candle(NAMED, "710.70")
    out = await run(monkeypatch, capsys, settings(f"{BENCHMARK},{NAMED}"))
    assert "OUTSIDE THE BAND" not in out
    assert "last traded outside the band" not in out
