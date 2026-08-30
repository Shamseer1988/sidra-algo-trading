from app.api.routes.health import DependencyHealth, HealthResponse


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
