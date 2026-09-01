from decimal import Decimal

from app.services.upstox_market_data import _ltpc, _number, _volume


def test_upstox_full_equity_feed_extracts_ltp_and_volume() -> None:
    ltpc, feed = _ltpc({"ff": {"marketFF": {"ltpc": {"ltp": 123.45}, "eFeedDetails": {"vtt": 400}}}})

    assert _number(ltpc["ltp"]) == Decimal("123.45")
    assert _volume(feed) == 400


def test_upstox_index_feed_extracts_ltp_without_volume() -> None:
    ltpc, feed = _ltpc({"ff": {"indexFF": {"ltpc": {"ltp": 24000}}}})

    assert _number(ltpc["ltp"]) == Decimal("24000")
    assert _volume(feed) is None
