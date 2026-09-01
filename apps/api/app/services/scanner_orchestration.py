"""Paper scanner orchestration: state, duplicate prevention, persistence and alerts."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.routes.events import SCANNER_EVENTS_CHANNEL
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.core.config import Settings
from app.db.models import ApplicationSetting, PaperSignal, TelegramAlert
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, SIGNALLED, STRATEGY_VERSION, evaluate_orb_retest
from app.services.safety import emergency_stop_state, paper_tracking_enabled
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry
from app.services.telegram import TelegramError, TelegramNotificationService
from app.services.telegram_config import configured_settings

STATE_TTL_SECONDS = 60 * 60 * 18


class PaperScannerOrchestrator:
    """Converts qualified completed candles into auditable paper signals only."""

    def __init__(self, settings: Settings, redis: Redis, benchmark_token: str | None = None) -> None:
        self._settings = settings
        self._redis = redis
        self._logger = structlog.get_logger("scanner.paper")
        self._benchmark_token = benchmark_token or settings.nifty_benchmark_token

    def _state_key(self, candle: CompletedCandle, strategy: StrategyConfiguration) -> str:
        return (
            f"scanner:strategy_state:{candle.session_date.isoformat()}:"
            f"{candle.instrument_token}:{strategy.id}:v{strategy.version}"
        )

    async def _controls(self) -> dict:
        async with SessionLocal() as session:
            setting = await session.get(ApplicationSetting, TRADING_KEY)
            return TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS).model_dump()

    async def _nifty_snapshot(self) -> dict:
        raw = await self._redis.get(f"market:indicator:{self._benchmark_token}")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    async def _can_signal(self, candle: CompletedCandle, controls: dict) -> bool:
        if not await paper_tracking_enabled(self._redis):
            return False
        if (await emergency_stop_state(self._redis)).get("active") == "true":
            return False
        async with SessionLocal() as session:
            count = await session.scalar(
                select(func.count(PaperSignal.id)).where(PaperSignal.session_date == candle.session_date)
            )
        return int(count or 0) < int(controls["maximum_signals"])

    async def _within_daily_risk_limit(self, candle: CompletedCandle, risk_amount, controls: dict) -> bool:
        daily_limit = (
            Decimal(str(controls["account_capital"]))
            * Decimal(str(controls["maximum_daily_risk_percent"]))
            / Decimal("100")
        )
        async with SessionLocal() as session:
            committed = await session.scalar(
                select(func.coalesce(func.sum(PaperSignal.risk_amount), 0)).where(
                    PaperSignal.session_date == candle.session_date
                )
            )
        return Decimal(str(committed or 0)) + Decimal(str(risk_amount)) <= daily_limit

    async def _record(
        self, candle: CompletedCandle, decision, indicators: dict, strategy: StrategyConfiguration
    ) -> PaperSignal | None:
        assert decision.side and decision.entry_price and decision.stop_price and decision.target_price
        assert decision.risk_amount is not None and decision.score_breakdown is not None
        signal_key = (
            f"{strategy.id}:v{strategy.version}:{candle.session_date.isoformat()}:"
            f"{candle.instrument_token}:{decision.side}"
        )
        signal = PaperSignal(
            signal_key=signal_key,
            instrument_token=candle.instrument_token,
            session_date=candle.session_date,
            candle_opened_at=candle.opened_at,
            strategy_version=f"{STRATEGY_VERSION}@{strategy.version}",
            side=decision.side,
            entry_price=decision.entry_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            quantity=decision.quantity,
            risk_amount=decision.risk_amount,
            score=decision.score,
            score_breakdown=decision.score_breakdown,
            indicator_snapshot=indicators,
        )
        async with SessionLocal() as session:
            session.add(signal)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(signal)
        return signal

    async def _alert(self, signal: PaperSignal) -> None:
        telegram_settings = await configured_settings(self._settings)
        if not telegram_settings.telegram_is_configured:
            return
        cooldown_key = f"telegram:paper_alert:{signal.signal_key}"
        if not await self._redis.set(cooldown_key, "1", ex=telegram_settings.telegram_alert_cooldown_seconds, nx=True):
            self._logger.info("scanner.telegram_alert_suppressed", signal_id=str(signal.id))
            return
        message = (
            "📄 PAPER SIGNAL\n"
            f"{signal.side} {signal.instrument_token}\n"
            f"Entry: {signal.entry_price} | Stop: {signal.stop_price} | Target: {signal.target_price}\n"
            f"Qty: {signal.quantity} | Score: {signal.score}/100\n"
            "Paper tracking only — no broker order has been sent."
        )
        try:
            result = await TelegramNotificationService(telegram_settings).send_trade_approval_request(
                str(signal.id), message
            )
            detail = "Telegram paper alert sent"
            status = "PAPER_ALERTED"
            message_id = str(result.get("result", {}).get("message_id", ""))
        except TelegramError:
            detail = "Telegram paper alert failed"
            status = "PAPER_RECORDED"
            message_id = None
            self._logger.warning("scanner.paper_alert_failed", signal_id=str(signal.id))
        async with SessionLocal() as session:
            stored = await session.get(PaperSignal, signal.id)
            if stored:
                stored.status = status
                stored.alert_detail = detail
            session.add(
                TelegramAlert(
                    correlation_id=signal.signal_key,
                    alert_type="PAPER_SIGNAL",
                    chat_id=telegram_settings.telegram_chat_id or "",
                    status="SENT" if status == "PAPER_ALERTED" else "FAILED",
                    telegram_message_id=message_id,
                    payload={"signal_id": str(signal.id), "side": signal.side, "instrument": signal.instrument_token},
                    failure_detail=None if status == "PAPER_ALERTED" else detail,
                )
            )
            await session.commit()

    async def on_completed_candle(self, candle: CompletedCandle, indicators: dict) -> None:
        if candle.instrument_token == self._benchmark_token:
            return
        controls = await self._controls()
        if not await self._can_signal(candle, controls):
            return
        nifty = await self._nifty_snapshot()
        async with SessionLocal() as session:
            strategies = await StrategyRegistry.enabled(session)
        for strategy in strategies:
            effective_controls = strategy.effective_controls(controls)
            state_key = self._state_key(candle, strategy)
            prior_state = await self._redis.get(state_key) or AWAITING
            decision = evaluate_orb_retest(candle, indicators, nifty, effective_controls, prior_state)
            if (
                decision.next_state == SIGNALLED
                and decision.risk_amount is not None
                and not await self._within_daily_risk_limit(candle, decision.risk_amount, effective_controls)
            ):
                decision = decision.__class__(next_state=AWAITING, reason="Daily paper-risk limit reached")
            await self._redis.set(state_key, decision.next_state, ex=STATE_TTL_SECONDS)
            if decision.next_state != SIGNALLED or decision.side is None:
                continue
            signal = await self._record(candle, decision, indicators, strategy)
            if signal is None:
                continue
            await self._redis.set(
                f"scanner:last_signal:{candle.instrument_token}",
                json.dumps({"signal_id": str(signal.id), "at": datetime.now(UTC).isoformat()}),
                ex=STATE_TTL_SECONDS,
            )
            await self._alert(signal)
            await self._redis.publish(
                SCANNER_EVENTS_CHANNEL,
                json.dumps(
                    {
                        "type": "paper_signal",
                        "signal_id": str(signal.id),
                        "instrument_token": signal.instrument_token,
                        "side": signal.side,
                        "status": signal.status,
                        "score": signal.score,
                    }
                ),
            )
            self._logger.info(
                "scanner.paper_signal_recorded",
                strategy_id=strategy.id,
                strategy_version=strategy.version,
                instrument_token=candle.instrument_token,
                side=signal.side,
                score=signal.score,
                live_trading_enabled=False,
            )
