import pytest
from pydantic import ValidationError

from app.api.routes import health as health_routes
from app.api.routes.health import DependencyHealth, HealthResponse
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls
from app.main import security_header_values
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
    assert controls.minimum_score == 80
    assert controls.minimum_rr >= 1.5
    assert controls.trade_start_time == "09:24"


def test_execution_approval_mode_ships_disabled() -> None:
    """The live path must be off in a freshly installed system."""
    assert TradingControls.model_validate(DEFAULT_TRADING_CONTROLS).execution_approval_mode == "DISABLED"


def test_settings_absent_from_the_database_still_default_to_disabled() -> None:
    """A stored row written before this field existed must not read as enabled."""
    stored = {key: value for key, value in DEFAULT_TRADING_CONTROLS.items() if key != "execution_approval_mode"}
    assert TradingControls.model_validate(stored).execution_approval_mode == "DISABLED"


@pytest.mark.parametrize(
    ("given", "expected"),
    [("automatic", "AUTOMATIC"), ("  telegram_approval  ", "TELEGRAM_APPROVAL"), ("Disabled", "DISABLED")],
)
def test_execution_approval_mode_is_normalised(given: str, expected: str) -> None:
    controls = TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, "execution_approval_mode": given})
    assert controls.execution_approval_mode == expected


@pytest.mark.parametrize("given", ["", "ENABLED", "yes", "AUTO", "TELEGRAM"])
def test_unrecognised_execution_approval_mode_is_rejected(given: str) -> None:
    """A typo must fail loudly rather than fall through to something permissive."""
    with pytest.raises(ValidationError):
        TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, "execution_approval_mode": given})


def test_firstock_paise_price_normalization() -> None:
    assert parse_price(139540) == "1395.4"
    assert parse_price("bad-price") is None
    assert parse_volume("123") == 123
    assert parse_volume("bad-volume") is None


def test_production_security_headers_are_strict_without_hsts_in_local_development() -> None:
    development = security_header_values("development")
    production = security_header_values("production")

    assert "frame-ancestors 'none'" in development["Content-Security-Policy"]
    assert development["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in development
    assert production["Strict-Transport-Security"].startswith("max-age=31536000")


async def test_readiness_report_preserves_paper_mode_and_never_reports_live_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def healthy_dependency() -> DependencyHealth:
        return DependencyHealth(status="healthy")

    monkeypatch.setattr(health_routes, "_database_health", healthy_dependency)
    monkeypatch.setattr(health_routes, "_redis_health", healthy_dependency)
    report = await health_routes.readiness_report()

    assert report.mode in {"PAPER", "REPLAY"}
    assert report.live_trading_enabled is False
