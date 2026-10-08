# --- a master that predates a field we now need ------------------------------
#
# On 7 October a deployment carrying the per-instrument tick fix still priced
# every order on the fallback grid. The master had been fetched the night
# before, which was recent enough not to be due, and had no tick sizes in it.
# Age alone could not see that.


def _record(entries: dict, *, hours_ago: float = 0.0):
    from datetime import UTC, datetime, timedelta

    from app.db.models import InstrumentMasterRefresh

    return InstrumentMasterRefresh(
        provider="UPSTOX",
        source_url="test",
        payload_sha256="x",
        instrument_count=len(entries),
        configured_keys=entries,
        missing_keys=[],
        fetched_at=datetime.now(UTC) - timedelta(hours=hours_ago),
    )


def test_a_master_without_tick_sizes_is_stale_however_recent():
    from app.services.upstox_instruments import _missing_fields

    assert _missing_fields(_record({"NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "isin": "x"}})) is True


def test_a_master_with_every_field_is_not_stale():
    from app.services.upstox_instruments import _missing_fields

    assert _missing_fields(_record({"NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0}})) is False


def test_one_incomplete_entry_makes_the_whole_record_stale():
    """Refetching costs one download the system already makes daily; trading on
    a partial grid costs orders."""
    from app.services.upstox_instruments import _missing_fields

    assert (
        _missing_fields(
            _record(
                {
                    "NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0},
                    "NSE_EQ|INE982J01020": {"trading_symbol": "PAYTM"},
                }
            )
        )
        is True
    )


def test_an_empty_master_is_judged_on_age_alone():
    """Nothing stored is not the same as something stored badly, and calling it
    stale would refetch on every loop forever."""
    from app.services.upstox_instruments import _missing_fields

    assert _missing_fields(_record({})) is False


# --- a master that predates an instrument we now subscribe to ----------------
#
# On 8 October RELIANCE and LT were added to UPSTOX_SUBSCRIPTIONS. The stored
# master had been fetched hours earlier: recent in age, complete in shape, and
# silent about both. Their ticks would have fallen back to ₹0.05 while both
# trade on ₹0.10, so roughly half of every price sent for them would have been
# refused outright -- the 7 October PAYTM rejection, in two more names.


def _settings(subscriptions: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        upstox_subscriptions=subscriptions,
        upstox_nifty_benchmark_key="NSE_INDEX|Nifty 50",
    )


STORED = {
    "NSE_INDEX|Nifty 50": {"trading_symbol": "NIFTY", "tick_size": 5.0},
    "NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0},
}


def test_an_instrument_added_since_the_last_fetch_makes_the_master_stale():
    from app.services.upstox_instruments import _uncovered_keys

    settings = _settings("NSE_EQ|INE335Y01020,NSE_EQ|INE002A01018")
    assert _uncovered_keys(settings, _record(STORED)) == ["NSE_EQ|INE002A01018"]


def test_a_master_covering_every_subscription_is_not_stale():
    from app.services.upstox_instruments import _uncovered_keys

    assert _uncovered_keys(_settings("NSE_EQ|INE335Y01020"), _record(STORED)) == []


def test_the_benchmark_counts_even_when_it_is_not_subscribed_by_hand():
    """It is force-added to the feed, so a master without it is incomplete."""
    from app.services.upstox_instruments import _uncovered_keys

    stored = {"NSE_EQ|INE335Y01020": {"trading_symbol": "IRCTC", "tick_size": 5.0}}
    assert "NSE_INDEX|Nifty 50" in _uncovered_keys(_settings("NSE_EQ|INE335Y01020"), _record(stored))


def test_a_key_upstox_does_not_publish_never_forces_another_fetch():
    """``missing_keys`` is absent from every fetch by definition. Treating it
    as uncovered would re-download the master on every check, forever."""
    from app.services.upstox_instruments import _uncovered_keys

    record = _record(STORED)
    record.missing_keys = ["NSE_EQ|DELISTED"]
    settings = _settings("NSE_EQ|INE335Y01020,NSE_EQ|DELISTED")
    assert _uncovered_keys(settings, record) == []


def test_an_empty_master_is_left_to_the_other_two_checks():
    """Nothing stored at all is already a refresh by age or by shape; reporting
    every subscription as uncovered would say the same thing twice."""
    from app.services.upstox_instruments import _uncovered_keys

    assert _uncovered_keys(_settings("NSE_EQ|INE335Y01020"), _record({})) == []
