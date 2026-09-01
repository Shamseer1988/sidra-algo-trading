from datetime import UTC, datetime
from decimal import Decimal

from app.services.candle_aggregation import normalize_market_timestamp
from app.services.upstox_market_data import _ltpc, _number, _volume


def test_upstox_full_equity_feed_extracts_ltp_and_volume() -> None:
    ltpc, feed = _ltpc({"ff": {"marketFF": {"ltpc": {"ltp": 123.45}, "eFeedDetails": {"vtt": 400}}}})

    assert _number(ltpc["ltp"]) == Decimal("123.45")
    assert _volume(feed) == 400


def test_upstox_index_feed_extracts_ltp_without_volume() -> None:
    ltpc, feed = _ltpc({"ff": {"indexFF": {"ltpc": {"ltp": 24000}}}})

    assert _number(ltpc["ltp"]) == Decimal("24000")
    assert _volume(feed) is None


def test_upstox_exchange_timestamp_uses_last_trade_milliseconds() -> None:
    fallback = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    expected = datetime(2026, 9, 1, 3, 59, 59, tzinfo=UTC)

    assert normalize_market_timestamp(int(expected.timestamp() * 1000), fallback) == expected
