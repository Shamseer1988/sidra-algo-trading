"""add paper-only OMS lifecycle and reconciliation records

Revision ID: 0013_oms_core
Revises: 0012_backtesting
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_oms_core"
down_revision = "0012_backtesting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=220), nullable=False),
        sa.Column("source_paper_signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_signals.id", ondelete="SET NULL")),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="PAPER"),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(length=20), nullable=False),
        sa.Column("intent_role", sa.String(length=20), nullable=False, server_default="ENTRY"),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("payload_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_order_intents_idempotency_key"),
        sa.UniqueConstraint("source_paper_signal_id", name="uq_order_intents_source_paper_signal_id"),
    )
    op.create_index("ix_order_intents_idempotency_key", "order_intents", ["idempotency_key"])
    op.create_index("ix_order_intents_mode", "order_intents", ["mode"])
    op.create_index("ix_order_intents_instrument_token", "order_intents", ["instrument_token"])
    op.create_index("ix_order_intents_created_at", "order_intents", ["created_at"])
    op.create_index("ix_order_intents_created_mode", "order_intents", ["created_at", "mode"])
    op.create_table(
        "oms_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_intent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order_intents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("venue", sa.String(length=40), nullable=False, server_default="PAPER_SIMULATOR"),
        sa.Column("broker_order_id", sa.String(length=120)),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="QUEUED"),
        sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_fill_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("last_error", sa.String(length=255)),
        sa.Column("unknown_since", sa.DateTime(timezone=True)),
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("order_intent_id", name="uq_oms_orders_intent"),
        sa.UniqueConstraint("broker_order_id"),
    )
    op.create_index("ix_oms_orders_order_intent_id", "oms_orders", ["order_intent_id"])
    op.create_index("ix_oms_orders_status", "oms_orders", ["status"])
    op.create_index("ix_oms_orders_status_updated", "oms_orders", ["status", "updated_at"])
    op.create_table(
        "oms_order_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("oms_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("oms_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=30)),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("oms_order_id", "sequence", name="uq_oms_order_events_sequence"),
    )
    op.create_index("ix_oms_order_events_oms_order_id", "oms_order_events", ["oms_order_id"])
    op.create_index("ix_oms_order_events_occurred_at", "oms_order_events", ["occurred_at"])
    op.create_index("ix_oms_order_events_order_occurred", "oms_order_events", ["oms_order_id", "occurred_at"])
    op.create_table(
        "execution_reconciliations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="PAPER"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="CLEAN"),
        sa.Column("internal_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("external_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_execution_reconciliations_mode", "execution_reconciliations", ["mode"])
    op.create_index("ix_execution_reconciliations_status", "execution_reconciliations", ["status"])
    op.create_index("ix_execution_reconciliations_created_at", "execution_reconciliations", ["created_at"])
    op.create_index("ix_execution_reconciliations_created_status", "execution_reconciliations", ["created_at", "status"])
    op.add_column("paper_orders", sa.Column("oms_order_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_paper_orders_oms_order_id", "paper_orders", "oms_orders", ["oms_order_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_paper_orders_oms_order_id", "paper_orders", ["oms_order_id"])


def downgrade() -> None:
    op.drop_constraint("uq_paper_orders_oms_order_id", "paper_orders", type_="unique")
    op.drop_constraint("fk_paper_orders_oms_order_id", "paper_orders", type_="foreignkey")
    op.drop_column("paper_orders", "oms_order_id")
    op.drop_table("execution_reconciliations")
    op.drop_table("oms_order_events")
    op.drop_table("oms_orders")
    op.drop_table("order_intents")
