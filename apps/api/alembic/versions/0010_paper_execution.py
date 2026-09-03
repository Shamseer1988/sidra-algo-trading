"""add paper order, fill, and position ledger

Revision ID: 0010_paper_execution
Revises: 0009_strategy_snapshots
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010_paper_execution"
down_revision = "0009_strategy_snapshots"
branch_labels = None
depends_on = None


def audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "paper_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_order_id", sa.String(length=180), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("strategy_version", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("order_type", sa.String(length=20), nullable=False),
        sa.Column("order_role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_fill_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("eligible_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("fee_total", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("rejection_reason", sa.String(length=255)),
        sa.Column("simulation_snapshot", sa.JSON(), nullable=False),
        *audit_columns(),
        sa.UniqueConstraint("client_order_id", name="uq_paper_orders_client_order_id"),
        sa.UniqueConstraint("paper_signal_id", "order_role", name="uq_paper_orders_signal_role"),
    )
    op.create_index("ix_paper_orders_paper_signal_id", "paper_orders", ["paper_signal_id"])
    op.create_index("ix_paper_orders_client_order_id", "paper_orders", ["client_order_id"], unique=True)
    op.create_index("ix_paper_orders_instrument_token", "paper_orders", ["instrument_token"])
    op.create_index("ix_paper_orders_session_date", "paper_orders", ["session_date"])
    op.create_index("ix_paper_orders_status", "paper_orders", ["status"])
    op.create_index("ix_paper_orders_eligible_after", "paper_orders", ["eligible_after"])
    op.create_index("ix_paper_orders_session_status", "paper_orders", ["session_date", "status"])
    op.create_index("ix_paper_orders_instrument_status", "paper_orders", ["instrument_token", "status"])
    op.create_table(
        "paper_fills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fill_key", sa.String(length=220), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("gross_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("slippage_amount", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("brokerage", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("stt", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("exchange_charge", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("gst", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("sebi_charge", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("stamp_duty", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("total_fees", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *audit_columns(),
        sa.UniqueConstraint("fill_key", name="uq_paper_fills_fill_key"),
    )
    op.create_index("ix_paper_fills_paper_order_id", "paper_fills", ["paper_order_id"])
    op.create_index("ix_paper_fills_fill_key", "paper_fills", ["fill_key"], unique=True)
    op.create_index("ix_paper_fills_instrument_token", "paper_fills", ["instrument_token"])
    op.create_index("ix_paper_fills_order_occurred", "paper_fills", ["paper_order_id", "occurred_at"])
    op.create_table(
        "paper_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("strategy_version", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPENING"),
        sa.Column("initial_quantity", sa.Integer(), nullable=False),
        sa.Column("open_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_entry_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("average_exit_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("current_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("target_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("fees_total", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("total_pnl", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        *audit_columns(),
        sa.UniqueConstraint("paper_signal_id", name="uq_paper_positions_paper_signal_id"),
    )
    op.create_index("ix_paper_positions_paper_signal_id", "paper_positions", ["paper_signal_id"])
    op.create_index("ix_paper_positions_instrument_token", "paper_positions", ["instrument_token"])
    op.create_index("ix_paper_positions_session_date", "paper_positions", ["session_date"])
    op.create_index("ix_paper_positions_status", "paper_positions", ["status"])
    op.create_index("ix_paper_positions_session_status", "paper_positions", ["session_date", "status"])
    op.create_index("ix_paper_positions_instrument_status", "paper_positions", ["instrument_token", "status"])


def downgrade() -> None:
    op.drop_table("paper_positions")
    op.drop_table("paper_fills")
    op.drop_table("paper_orders")
