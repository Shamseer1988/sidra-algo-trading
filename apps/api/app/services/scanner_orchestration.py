"""Paper scanner orchestration: state, duplicate prevention, persistence and alerts."""

import json
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.routes.events import SCANNER_EVENTS_CHANNEL
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.core.config import Settings
from app.db.models import ApplicationSetting, PaperSignal, ScannerEvaluation, TelegramAlert
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperOrderManager
from app.services.paper_strategy import AWAITING, SIGNALLED
from app.services.risk_engine import PaperRiskEngine
from app.services.safety import emergency_stop_state, paper_tracking_enabled
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry
from app.services.telegram import TelegramError, TelegramNotificationService
from app.services.telegram_config import configured_settings
from app.services.trading_symbols import resolve_script_name

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
                select(func.count(PaperSignal.id)).where(
                    PaperSignal.session_date == candle.session_date,
                    PaperSignal.status.notin_(["PAPER_RISK_REJECTED"]),
                )
            )
        return int(count or 0) < int(controls["maximum_signals"])

    async def _strategy_limit_reason(self, candle: CompletedCandle, strategy: StrategyConfiguration) -> str | None:
        """Return a paper-only configuration limit violation before recording a signal."""
        async with SessionLocal() as session:
            accepted = await session.scalar(
                select(func.count(ScannerEvaluation.id)).where(
                    ScannerEvaluation.session_date == candle.session_date,
                    ScannerEvaluation.strategy_id == strategy.id,
                    ScannerEvaluation.status == "ACCEPTED",
                )
            )
            if int(accepted or 0) >= strategy.max_trades_per_day:
                return "Strategy maximum paper trades reached"
            if strategy.cooldown_minutes:
                latest = await session.scalar(
                    select(func.max(ScannerEvaluation.candle_opened_at)).where(
                        ScannerEvaluation.session_date == candle.session_date,
                        ScannerEvaluation.strategy_id == strategy.id,
                        ScannerEvaluation.status == "ACCEPTED",
                    )
                )
        if latest and (candle.opened_at - latest).total_seconds() < strategy.cooldown_minutes * 60:
            return "Strategy paper-signal cooldown is active"
        return None

    async def _record(
        self,
        candle: CompletedCandle,
        decision,
        indicators: dict,
        strategy: StrategyConfiguration,
        strategy_snapshot: dict,
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
            strategy_version=f"{strategy.strategy_type}@{strategy.version}",
            side=decision.side,
            entry_price=decision.entry_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            quantity=decision.quantity,
            risk_amount=decision.risk_amount,
            score=decision.score,
            score_breakdown=decision.score_breakdown,
            strategy_snapshot=strategy_snapshot,
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

    async def _mark_signal_risk_block(self, signal: PaperSignal, reason: str) -> None:
        async with SessionLocal() as session:
            stored = await session.get(PaperSignal, signal.id)
            if stored:
                stored.status = "PAPER_RISK_REJECTED"
                stored.alert_detail = reason
                await session.commit()

    @staticmethod
    def _evaluation_status(decision) -> str:
        if decision.next_state == SIGNALLED and decision.side:
            return "ACCEPTED"
        if decision.reason and decision.reason.lower().startswith("awaiting"):
            return "WATCHING"
        return "REJECTED"

    @staticmethod
    def _failed_conditions(decision) -> list[str]:
        failed = [] if decision.next_state == SIGNALLED else [decision.reason or "Strategy conditions were not met"]
        failed.extend(
            key.replace("_", " ").title() for key, value in (decision.score_breakdown or {}).items() if value == 0
        )
        return list(dict.fromkeys(failed))

    async def _record_evaluation(
        self,
        candle: CompletedCandle,
        indicators: dict,
        strategy: StrategyConfiguration,
        decision,
        strategy_snapshot: dict,
    ):
        quality = indicators.get("data_quality") if isinstance(indicators.get("data_quality"), dict) else {}
        evaluation = ScannerEvaluation(
            evaluation_key=(
                f"{strategy.id}:v{strategy.version}:{candle.session_date.isoformat()}:"
                f"{candle.instrument_token}:{candle.opened_at.isoformat()}"
            ),
            instrument_token=candle.instrument_token,
            session_date=candle.session_date,
            candle_opened_at=candle.opened_at,
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            status=self._evaluation_status(decision),
            decision_state=decision.next_state,
            side=decision.side,
            reason=decision.reason or "Strategy evaluation completed",
            failed_conditions=self._failed_conditions(decision),
            data_quality_state=str(quality.get("state", "MISSING")),
            candle_close=candle.close,
            candle_volume=candle.volume,
            score=decision.score,
            score_breakdown=decision.score_breakdown or {},
            strategy_snapshot=strategy_snapshot,
            indicator_snapshot=indicators,
            entry_price=decision.entry_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            quantity=decision.quantity or None,
            risk_amount=decision.risk_amount,
        )
        async with SessionLocal() as session:
            session.add(evaluation)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(evaluation)
        return evaluation

    async def _record_quality_block(self, candle: CompletedCandle, indicators: dict):
        quality = indicators.get("data_quality") if isinstance(indicators.get("data_quality"), dict) else {}
        reason = str(quality.get("reason", "Quality snapshot unavailable"))
        evaluation = ScannerEvaluation(
            evaluation_key=f"data-quality:{candle.session_date.isoformat()}:{candle.instrument_token}:{candle.opened_at.isoformat()}",
            instrument_token=candle.instrument_token,
            session_date=candle.session_date,
            candle_opened_at=candle.opened_at,
            strategy_id="data-quality",
            strategy_name="Data quality gate",
            strategy_version=1,
            status="REJECTED",
            decision_state="BLOCKED",
            reason=reason,
            failed_conditions=[reason],
            data_quality_state=str(quality.get("state", "MISSING")),
            candle_close=candle.close,
            candle_volume=candle.volume,
            strategy_snapshot={},
            indicator_snapshot=indicators,
        )
        async with SessionLocal() as session:
            session.add(evaluation)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(evaluation)
        return evaluation

    async def _publish_evaluation(self, evaluation: ScannerEvaluation) -> None:
        await self._redis.publish(
            SCANNER_EVENTS_CHANNEL,
            json.dumps(
                {
                    "type": "scanner_evaluation",
                    "evaluation_id": str(evaluation.id),
                    "instrument_token": evaluation.instrument_token,
                    "status": evaluation.status,
                    "score": evaluation.score,
                }
            ),
        )

    async def _alert(self, signal: PaperSignal) -> None:
        telegram_settings = await configured_settings(self._settings)
        if not telegram_settings.telegram_is_configured:
            return
        cooldown_key = f"telegram:paper_alert:{signal.signal_key}"
        if not await self._redis.set(cooldown_key, "1", ex=telegram_settings.telegram_alert_cooldown_seconds, nx=True):
            self._logger.info("scanner.telegram_alert_suppressed", signal_id=str(signal.id))
            return

        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(UTC).astimezone(ist).strftime("%d-%b-%Y %I:%M:%S %p")
        script_name = resolve_script_name(signal.instrument_token)

        entry = float(signal.entry_price)
        stop = float(signal.stop_price)
        target = float(signal.target_price)
        qty = int(signal.quantity)

        risk_pts = abs(entry - stop)
        reward_pts = abs(target - entry)
        risk_pct = (risk_pts / entry * 100) if entry > 0 else 0.0
        target_pct = (reward_pts / entry * 100) if entry > 0 else 0.0
        rr_ratio = (reward_pts / risk_pts) if risk_pts > 0 else 0.0
        total_val = entry * qty
        margin_5x = total_val / 5.0
        risk_amount = float(signal.risk_amount or 0.0)

        side_icon = "🟢" if signal.side == "LONG" else "🔴"
        side_text = "BUY / LONG" if signal.side == "LONG" else "SELL / SHORT"

        message = (
            "🎯 <b>TRADING SIGNAL MATCHED</b>\n\n"
            f"📊 <b>Stock:</b> <b>{script_name}</b> (<code>{signal.instrument_token}</code>)\n"
            f"📈 <b>Direction:</b> {side_icon} <b>{side_text}</b>\n"
            f"🧠 <b>Strategy:</b> {signal.strategy_version}\n"
            f"⭐ <b>Score:</b> {signal.score} / 100\n\n"
            f"💵 <b>Entry Price:</b> ₹{entry:,.2f}\n"
            f"🛑 <b>Stop Loss:</b> ₹{stop:,.2f} ({'-' if signal.side == 'LONG' else '+'}{risk_pts:.2f} pts | {risk_pct:.2f}%)\n"
            f"🎯 <b>Target:</b> ₹{target:,.2f} ({'+' if signal.side == 'LONG' else '-'}{reward_pts:.2f} pts | {target_pct:.2f}%)\n"
            f"⚖️ <b>Risk : Reward:</b> 1 : {rr_ratio:.2f}\n\n"
            f"📦 <b>Quantity:</b> {qty:,} shares\n"
            f"💰 <b>Total Exposure:</b> ₹{total_val:,.2f} (<i>5x Intraday Margin:</i> ₹{margin_5x:,.2f})\n"
            f"🛡️ <b>Risk Allocated:</b> ₹{risk_amount:,.2f}\n\n"
            f"⏰ <b>Time:</b> {now_ist} IST\n"
            "⚠️ <i>Paper tracking simulation — ready for confirmation.</i>"
        )
        try:
            tg = TelegramNotificationService(telegram_settings)
            result = await tg.send_trade_approval_request(str(signal.id), message)
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
        quality = indicators.get("data_quality")
        if not isinstance(quality, dict) or quality.get("state") in {"INVALID", "STALE", None}:
            evaluation = await self._record_quality_block(candle, indicators)
            if evaluation:
                await self._publish_evaluation(evaluation)
            self._logger.warning(
                "scanner.signal_blocked_data_quality",
                instrument_token=candle.instrument_token,
                state=quality.get("state") if isinstance(quality, dict) else "MISSING",
                reason=quality.get("reason") if isinstance(quality, dict) else "Quality snapshot unavailable",
            )
            return
        controls = await self._controls()
        if not await self._can_signal(candle, controls):
            return
        nifty = await self._nifty_snapshot()
        async with SessionLocal() as session:
            strategies = await StrategyRegistry.enabled(session)
        for strategy in strategies:
            effective_controls = strategy.effective_controls(controls)
            strategy_snapshot = strategy.snapshot(controls)
            state_key = self._state_key(candle, strategy)
            prior_state = await self._redis.get(state_key) or AWAITING
            decision = StrategyRegistry.evaluate(strategy, candle, indicators, nifty, effective_controls, prior_state)
            if decision.next_state == SIGNALLED:
                limit_reason = await self._strategy_limit_reason(candle, strategy)
                if limit_reason:
                    decision = decision.__class__(next_state=AWAITING, reason=limit_reason)
            await self._redis.set(state_key, decision.next_state, ex=STATE_TTL_SECONDS)
            evaluation = await self._record_evaluation(candle, indicators, strategy, decision, strategy_snapshot)
            if evaluation:
                await self._publish_evaluation(evaluation)
            if decision.next_state != SIGNALLED or decision.side is None:
                continue
            signal = await self._record(candle, decision, indicators, strategy, strategy_snapshot)
            if signal is None:
                continue
            try:
                risk = await PaperRiskEngine().reserve_signal(signal)
                if not risk.allowed:
                    await self._mark_signal_risk_block(signal, risk.reason)
                    self._logger.info(
                        "scanner.paper_signal_risk_rejected", signal_id=str(signal.id), reason=risk.reason
                    )
                    continue
                await PaperOrderManager().queue_signal(signal)
            except Exception:
                await self._mark_signal_risk_block(signal, "Paper risk reservation unavailable")
                self._logger.exception("scanner.paper_order_queue_failed", signal_id=str(signal.id))
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
