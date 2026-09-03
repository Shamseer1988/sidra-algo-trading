"""The NIFTY benchmark must always be part of the live feed subscription set.

Relative strength and NIFTY regime scoring silently collapse to zero when the
benchmark index is not streamed, so the feed helpers force-include it whenever at
least one equity instrument is configured.
"""

from app.core.config import Settings
from app.services.firstock.market_data import feed_subscriptions as firstock_feed_subscriptions
from app.services.upstox_market_data import feed_subscriptions as upstox_feed_subscriptions


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://sidra:secret@postgres/sidra",
        "redis_url": "redis://redis:6379/0",
        "jwt_secret": "a-secure-test-secret-that-is-longer-than-32-characters",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_upstox_feed_forces_the_benchmark_key_when_missing() -> None:
    settings = _settings(
        upstox_subscriptions="NSE_EQ|INE002A01018,NSE_EQ|INE467B01029",
        upstox_nifty_benchmark_key="NSE_INDEX|Nifty 50",
    )
    subscriptions = upstox_feed_subscriptions(settings)
    assert subscriptions[0] == "NSE_INDEX|Nifty 50"
    assert subscriptions.count("NSE_INDEX|Nifty 50") == 1
    assert "NSE_EQ|INE002A01018" in subscriptions


def test_upstox_feed_keeps_an_explicitly_configured_benchmark_in_place() -> None:
    settings = _settings(
        upstox_subscriptions="NSE_INDEX|Nifty 50,NSE_EQ|INE002A01018",
        upstox_nifty_benchmark_key="NSE_INDEX|Nifty 50",
    )
    assert upstox_feed_subscriptions(settings) == ["NSE_INDEX|Nifty 50", "NSE_EQ|INE002A01018"]


def test_upstox_feed_stays_empty_without_any_configured_instrument() -> None:
    assert upstox_feed_subscriptions(_settings(upstox_subscriptions="")) == []


def test_firstock_feed_forces_the_benchmark_token_when_missing() -> None:
    settings = _settings(firstock_subscriptions="NSE:2885|NSE:11536", nifty_benchmark_token="NSE:26000")
    subscriptions = firstock_feed_subscriptions(settings)
    assert subscriptions[0] == "NSE:26000"
    assert subscriptions.count("NSE:26000") == 1
    assert "NSE:2885" in subscriptions


def test_firstock_feed_stays_empty_without_any_configured_instrument() -> None:
    assert firstock_feed_subscriptions(_settings(firstock_subscriptions="")) == []
