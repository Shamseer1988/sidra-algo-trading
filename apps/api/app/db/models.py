import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(enum.StrEnum):
    ADMIN = "ADMIN"
    TRADER = "TRADER"
    VIEWER = "VIEWER"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), default=UserRole.ADMIN)
    is_active: Mapped[bool] = mapped_column(default=True)
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)


class LoginHistory(Base):
    __tablename__ = "login_history"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email_attempted: Mapped[str] = mapped_column(String(320), index=True)
    success: Mapped[bool]
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_event", "created_at", "event_type"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplicationSetting(TimestampMixin, Base):
    """Versioned application settings. Secrets are never persisted in this table."""

    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class BrokerCredential(TimestampMixin, Base):
    """Encrypted server-side connector credential; never serialized by an API route."""

    __tablename__ = "broker_credentials"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class InstrumentMasterRefresh(Base):
    __tablename__ = "instrument_master_refreshes"
    __table_args__ = (Index("ix_instrument_master_refreshes_provider_fetched", "provider", "fetched_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload_sha256: Mapped[str] = mapped_column(String(64))
    instrument_count: Mapped[int] = mapped_column(Integer)
    configured_keys: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_keys: Mapped[list] = mapped_column(JSON, default=list)


class TelegramAlert(TimestampMixin, Base):
    __tablename__ = "telegram_alerts"
    __table_args__ = (Index("ix_telegram_alerts_created_status", "created_at", "status"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    correlation_id: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    alert_type: Mapped[str] = mapped_column(String(60), index=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), index=True)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TelegramInboundEvent(Base):
    __tablename__ = "telegram_inbound_events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    telegram_update_id: Mapped[int] = mapped_column(unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted: Mapped[bool] = mapped_column(default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TradeApprovalIntent(TimestampMixin, Base):
    __tablename__ = "trade_approval_intents"
    __table_args__ = (Index("ix_trade_approval_intents_status_created", "status", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    reference_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    decision: Mapped[str] = mapped_column(String(20), index=True)
    source: Mapped[str] = mapped_column(String(30))
    requester_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="RECORDED", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    risk_revalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_block_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class LiveReadinessCheck(Base):
    """Immutable record of a live-readiness review; it cannot activate trading."""

    __tablename__ = "live_readiness_checks"
    __table_args__ = (Index("ix_live_readiness_checks_created_status", "created_at", "status"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(30), default="HARD_LOCKED", index=True)
    overall_ready: Mapped[bool] = mapped_column(default=False)
    gate_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    checked_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MarketCandle(Base):
    """An immutable, completed OHLCV candle; no partial candles are stored here."""

    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("instrument_token", "timeframe_seconds", "opened_at", name="uq_market_candle_bucket"),
        Index("ix_market_candles_instrument_opened", "instrument_token", "opened_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    tick_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MarketIndicatorSnapshot(Base):
    """Versionable derived values for the latest completed candle of an instrument."""

    __tablename__ = "market_indicator_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument_token", "timeframe_seconds", "candle_opened_at", name="uq_market_indicator_snapshot"
        ),
        Index("ix_market_indicator_snapshots_instrument_created", "instrument_token", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    candle_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PaperSignal(TimestampMixin, Base):
    """A scanner decision for paper tracking only; it is not an order or position."""

    __tablename__ = "paper_signals"
    __table_args__ = (
        UniqueConstraint("signal_key", name="uq_paper_signal_key"),
        Index("ix_paper_signals_session_status", "session_date", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    signal_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    candle_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(30), default="PAPER_RECORDED", index=True)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    stop_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    target_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    quantity: Mapped[int] = mapped_column(Integer)
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    score: Mapped[int] = mapped_column(Integer)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    strategy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    indicator_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    alert_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ScannerEvaluation(TimestampMixin, Base):
    """One auditable strategy evaluation, including non-signalling decisions."""

    __tablename__ = "scanner_evaluations"
    __table_args__ = (
        UniqueConstraint("evaluation_key", name="uq_scanner_evaluation_key"),
        Index("ix_scanner_evaluations_session_status", "session_date", "status"),
        Index("ix_scanner_evaluations_instrument_created", "instrument_token", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    evaluation_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    candle_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    strategy_name: Mapped[str] = mapped_column(String(120))
    strategy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    decision_state: Mapped[str] = mapped_column(String(40))
    side: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    failed_conditions: Mapped[list] = mapped_column(JSON, default=list)
    data_quality_state: Mapped[str] = mapped_column(String(20), default="MISSING")
    candle_close: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    candle_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    strategy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    indicator_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)


class PaperSignalOutcome(TimestampMixin, Base):
    """Paper-only post-signal outcome derived from later completed candles."""

    __tablename__ = "paper_signal_outcomes"
    __table_args__ = (UniqueConstraint("paper_signal_id", name="uq_paper_signal_outcome_signal"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    paper_signal_id: Mapped[UUID] = mapped_column(ForeignKey("paper_signals.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    realized_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    mfe_r: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    mae_r: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)


class PaperOrder(TimestampMixin, Base):
    """A simulated order only. It has no broker identifier or submission path."""

    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_paper_orders_client_order_id"),
        UniqueConstraint("paper_signal_id", "order_role", name="uq_paper_orders_signal_role"),
        Index("ix_paper_orders_session_status", "session_date", "status"),
        Index("ix_paper_orders_instrument_status", "instrument_token", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    paper_signal_id: Mapped[UUID] = mapped_column(ForeignKey("paper_signals.id", ondelete="CASCADE"), index=True)
    oms_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("oms_orders.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    client_order_id: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(10), index=True)
    order_type: Mapped[str] = mapped_column(String(20))
    order_role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    eligible_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fee_total: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    simulation_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class PaperFill(TimestampMixin, Base):
    """An immutable simulated fill and its complete cost breakdown."""

    __tablename__ = "paper_fills"
    __table_args__ = (
        UniqueConstraint("fill_key", name="uq_paper_fills_fill_key"),
        Index("ix_paper_fills_order_occurred", "paper_order_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    paper_order_id: Mapped[UUID] = mapped_column(ForeignKey("paper_orders.id", ondelete="CASCADE"), index=True)
    fill_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    gross_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    slippage_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    brokerage: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    stt: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    exchange_charge: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    gst: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    sebi_charge: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    stamp_duty: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    total_fees: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PaperPosition(TimestampMixin, Base):
    """One paper position per source signal, never a broker-held position."""

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("paper_signal_id", name="uq_paper_positions_paper_signal_id"),
        Index("ix_paper_positions_session_status", "session_date", "status"),
        Index("ix_paper_positions_instrument_status", "instrument_token", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    paper_signal_id: Mapped[UUID] = mapped_column(ForeignKey("paper_signals.id", ondelete="CASCADE"), index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPENING", index=True)
    initial_quantity: Mapped[int] = mapped_column(Integer)
    open_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    average_exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    target_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    fees_total: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskReservation(TimestampMixin, Base):
    """A durable paper-risk allocation; it is never a broker margin reservation."""

    __tablename__ = "risk_reservations"
    __table_args__ = (
        UniqueConstraint("paper_signal_id", name="uq_risk_reservations_paper_signal_id"),
        Index("ix_risk_reservations_session_status", "session_date", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    paper_signal_id: Mapped[UUID] = mapped_column(ForeignKey("paper_signals.id", ondelete="CASCADE"), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    decision_reason: Mapped[str] = mapped_column(String(255), default="Paper risk reserved")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestRun(TimestampMixin, Base):
    """Immutable-input, paper-only historical research run; never an execution request."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_created_status", "created_at", "status"),
        Index("ix_backtest_runs_dates", "start_date", "end_date"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="COMPLETED", index=True)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer)
    instrument_tokens: Mapped[list] = mapped_column(JSON, default=list)
    strategy_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    controls_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    data_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_candle_count: Mapped[int] = mapped_column(Integer, default=0)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    final_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    net_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BacktestTrade(Base):
    """One completed simulated trade emitted by a durable backtest run."""

    __tablename__ = "backtest_trades"
    __table_args__ = (
        UniqueConstraint("run_id", "trade_key", name="uq_backtest_trades_run_key"),
        Index("ix_backtest_trades_run_exited", "run_id", "exited_at"),
        Index("ix_backtest_trades_strategy", "strategy_id", "strategy_version"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    trade_key: Mapped[str] = mapped_column(String(220))
    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    strategy_name: Mapped[str] = mapped_column(String(120))
    strategy_version: Mapped[int] = mapped_column(Integer)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    fees_total: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    realized_r: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    exit_reason: Mapped[str] = mapped_column(String(40))


class OrderIntent(Base):
    """Immutable request to execute within one mode; never a broker submission itself."""

    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_order_intents_idempotency_key"),
        UniqueConstraint("source_paper_signal_id", name="uq_order_intents_source_paper_signal_id"),
        Index("ix_order_intents_created_mode", "created_at", "mode"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    source_paper_signal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("paper_signals.id", ondelete="SET NULL"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(20), default="PAPER", index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(20))
    intent_role: Mapped[str] = mapped_column(String(20), default="ENTRY")
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    payload_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class OmsOrder(TimestampMixin, Base):
    """Execution lifecycle record. Broker identifiers remain null in the paper-only release."""

    __tablename__ = "oms_orders"
    __table_args__ = (
        UniqueConstraint("order_intent_id", name="uq_oms_orders_intent"),
        Index("ix_oms_orders_status_updated", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.id", ondelete="RESTRICT"), unique=True, index=True
    )
    venue: Mapped[str] = mapped_column(String(40), default="PAPER_SIMULATOR")
    broker_order_id: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_transition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OmsOrderEvent(Base):
    """Append-only lifecycle event; sequence is unique within an OMS order."""

    __tablename__ = "oms_order_events"
    __table_args__ = (
        UniqueConstraint("oms_order_id", "sequence", name="uq_oms_order_events_sequence"),
        Index("ix_oms_order_events_order_occurred", "oms_order_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    oms_order_id: Mapped[UUID] = mapped_column(ForeignKey("oms_orders.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30))
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ExecutionReconciliation(Base):
    """A bounded comparison checkpoint; paper mode has no external broker side."""

    __tablename__ = "execution_reconciliations"
    __table_args__ = (Index("ix_execution_reconciliations_created_status", "created_at", "status"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    mode: Mapped[str] = mapped_column(String(20), default="PAPER", index=True)
    status: Mapped[str] = mapped_column(String(30), default="CLEAN", index=True)
    internal_orders: Mapped[int] = mapped_column(Integer, default=0)
    external_orders: Mapped[int] = mapped_column(Integer, default=0)
    unknown_orders: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ShadowOrder(TimestampMixin, Base):
    """A zero-submission intended order compared only with local paper execution."""

    __tablename__ = "shadow_orders"
    __table_args__ = (
        UniqueConstraint("oms_order_id", name="uq_shadow_orders_oms_order_id"),
        Index("ix_shadow_orders_status_updated", "comparison_status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    oms_order_id: Mapped[UUID] = mapped_column(ForeignKey("oms_orders.id", ondelete="CASCADE"), unique=True, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(10))
    intended_quantity: Mapped[int] = mapped_column(Integer)
    intended_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    comparison_status: Mapped[str] = mapped_column(String(30), default="AWAITING_PAPER_FILL", index=True)
    paper_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    price_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    compared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScanUniverseEntry(Base):
    """One ranked instrument in a session's dynamic scan universe.

    The scanner only generates signals for ``selected`` instruments when
    ``UNIVERSE_ENABLED`` is set; otherwise this table is advisory only.
    """

    __tablename__ = "scan_universe"
    __table_args__ = (
        UniqueConstraint("session_date", "instrument_token", name="uq_scan_universe_session_instrument"),
        Index("ix_scan_universe_session_selected", "session_date", "selected"),
        Index("ix_scan_universe_session_rank", "session_date", "rank"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    instrument_token: Mapped[str] = mapped_column(String(64), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    selected: Mapped[bool] = mapped_column(default=False)
    eligible: Mapped[bool] = mapped_column(default=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    liquidity_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    volatility_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    gap_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    trend_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
