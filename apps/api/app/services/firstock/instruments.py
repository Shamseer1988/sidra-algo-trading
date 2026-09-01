from dataclasses import dataclass

from app.core.config import Settings
from app.services.firstock.market_data import configured_subscriptions


@dataclass(frozen=True)
class FirstockInstrumentSubscription:
    exchange: str
    token: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.token}"


class FirstockInstrumentService:
    """Owns the configured instrument universe until master-file import is implemented.

    Firstock's current V2 feed contract consumes exchange/token pairs. Search/master
    import will be added only after its response contract is verified against the
    active account, preventing guessed or stale instrument identifiers.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def subscriptions(self) -> list[FirstockInstrumentSubscription]:
        instruments: list[FirstockInstrumentSubscription] = []
        for raw_token in configured_subscriptions(self._settings):
            exchange, separator, token = raw_token.partition(":")
            if separator and exchange and token:
                instruments.append(FirstockInstrumentSubscription(exchange=exchange, token=token))
        return instruments
