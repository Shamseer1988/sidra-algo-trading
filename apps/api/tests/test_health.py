from app.api.routes.health import DependencyHealth, HealthResponse
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls
from app.services.firstock.market_data import parse_price, parse_volume


def test_health_response_contract() -> None:
    response = HealthResponse(
        status="degraded",
        mode="PAPER",
        live_trading_enabled=False,
        timestamp="2026-01-01T00:00:00Z",
        database=DependencyHealth(status="offline"),
        redis=DependencyHealth(status="offline"),
    )
    assert response.live_trading_enabled is False
    assert response.mode == "PAPER"


def test_default_trading_controls_are_valid_and_paper_safe() -> None:
    controls = TradingControls.model_validate(DEFAULT_TRADING_CONTROLS)
    assert controls.minimum_score == 90
    assert controls.minimum_rr >= 1.5
    assert controls.trade_start_time == "09:24"


def test_firstock_paise_price_normalization() -> None:
    assert parse_price(139540) == "1395.4"
    assert parse_price("bad-price") is None
    assert parse_volume("123") == 123
    assert parse_volume("bad-volume") is None
