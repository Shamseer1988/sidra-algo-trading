from app.services.market_calculations import compose_market_regime, india_vix_state, market_breadth

VIX_BOUNDS = {"calm_below": 13.0, "stressed_above": 18.0, "extreme_above": 25.0}


def test_india_vix_state_classifies_by_level_and_change() -> None:
    assert india_vix_state(None, None, **VIX_BOUNDS) is None
    calm = india_vix_state(11.0, 10.0, **VIX_BOUNDS)
    assert calm["state"] == "CALM" and calm["change_percent"] == 10.0
    assert india_vix_state(15.0, 15.0, **VIX_BOUNDS)["state"] == "NORMAL"
    assert india_vix_state(20.0, 16.0, **VIX_BOUNDS)["state"] == "STRESSED"
    assert india_vix_state(30.0, 20.0, **VIX_BOUNDS)["state"] == "EXTREME"


def test_market_breadth_states() -> None:
    assert market_breadth(0, 0) is None
    assert market_breadth(7, 10)["state"] == "EXPANSION"
    assert market_breadth(3, 10)["state"] == "CONTRACTION"
    assert market_breadth(5, 10)["state"] == "MIXED"


def test_compose_regime_is_risk_on_when_structure_breadth_and_vix_align() -> None:
    regime = compose_market_regime(
        "BULLISH",
        0.8,
        india_vix_state(12.0, 12.5, **VIX_BOUNDS),
        market_breadth(8, 10),
    )
    assert regime["regime"] == "RISK_ON"
    assert regime["allow_long"] is True
    assert regime["allow_short"] is False  # do not short a clean risk-on tape
    assert regime["size_multiplier"] == 1.0


def test_compose_regime_blocks_longs_and_halves_size_when_stressed() -> None:
    regime = compose_market_regime(
        "BEARISH",
        -1.2,
        india_vix_state(21.0, 17.0, **VIX_BOUNDS),
        market_breadth(2, 10),
    )
    assert regime["regime"] == "RISK_OFF"
    assert regime["allow_long"] is False
    assert regime["allow_short"] is True
    assert regime["size_multiplier"] == 0.5


def test_compose_regime_stands_aside_when_vix_is_extreme() -> None:
    regime = compose_market_regime(
        "NEUTRAL",
        0.0,
        india_vix_state(28.0, 20.0, **VIX_BOUNDS),
        market_breadth(5, 10),
    )
    assert regime["allow_long"] is False
    assert regime["allow_short"] is False
    assert regime["size_multiplier"] == 0.0
    assert "extreme" in regime["reason"].lower()


def test_compose_regime_is_neutral_with_no_inputs() -> None:
    regime = compose_market_regime(None, None, None, None)
    assert regime["regime"] == "NEUTRAL"
    assert regime["allow_long"] is True
    assert regime["allow_short"] is True
    assert regime["size_multiplier"] == 1.0
