import pytest
from pydantic import ValidationError

from app.services.broker_controls import BrokerControls


def test_upstox_is_the_default_paper_market_data_connector() -> None:
    controls = BrokerControls()

    assert controls.active_broker == "UPSTOX"
    assert controls.upstox_paper_enabled is True
    assert controls.firstock_feed_enabled is False


def test_firstock_can_be_selected_as_the_single_feed() -> None:
    controls = BrokerControls(upstox_paper_enabled=False, firstock_feed_enabled=True)

    assert controls.active_broker == "FIRSTOCK"


def test_both_connectors_cannot_be_enabled_together() -> None:
    with pytest.raises(ValidationError, match="Only one market-data connector"):
        BrokerControls(upstox_paper_enabled=True, firstock_feed_enabled=True)
