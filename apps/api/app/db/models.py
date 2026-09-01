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
    indicator_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    alert_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)


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
