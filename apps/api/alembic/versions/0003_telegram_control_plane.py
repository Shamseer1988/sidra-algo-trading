"""add telegram and approval control-plane records

Revision ID: 0003_telegram_control_plane
Revises: 0002_application_settings
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_telegram_control_plane"
down_revision = "0002_application_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("correlation_id", sa.String(length=120), unique=True),
        sa.Column("alert_type", sa.String(length=60), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("telegram_message_id", sa.String(length=64)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("failure_detail", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_telegram_alerts_alert_type", "telegram_alerts", ["alert_type"])
    op.create_index("ix_telegram_alerts_status", "telegram_alerts", ["status"])
    op.create_index("ix_telegram_alerts_created_status", "telegram_alerts", ["created_at", "status"])
    op.create_table(
        "telegram_inbound_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_update_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("sender_id", sa.String(length=64)),
        sa.Column("chat_id", sa.String(length=64)),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_telegram_inbound_events_telegram_update_id", "telegram_inbound_events", ["telegram_update_id"])
    op.create_index("ix_telegram_inbound_events_event_type", "telegram_inbound_events", ["event_type"])
    op.create_index("ix_telegram_inbound_events_created_at", "telegram_inbound_events", ["created_at"])
    op.create_table(
        "trade_approval_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference_id", sa.String(length=120), unique=True, nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("requester_id", sa.String(length=64)),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_trade_approval_intents_reference_id", "trade_approval_intents", ["reference_id"])
    op.create_index("ix_trade_approval_intents_decision", "trade_approval_intents", ["decision"])
    op.create_index("ix_trade_approval_intents_status", "trade_approval_intents", ["status"])
    op.create_index("ix_trade_approval_intents_status_created", "trade_approval_intents", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("trade_approval_intents")
    op.drop_table("telegram_inbound_events")
    op.drop_table("telegram_alerts")
