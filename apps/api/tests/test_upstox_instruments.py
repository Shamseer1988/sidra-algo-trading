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
