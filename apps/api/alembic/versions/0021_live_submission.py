"""add live activation, submission and approval records

Revision ID: 0021_live_submission
Revises: 0020_live_shadow
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0021_live_submission"
down_revision = "0020_live_shadow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An administrator's armed window. It expires on its own, so the default
    # state of the system at any future moment is off rather than on.
    op.create_table(
        "live_activations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "activated_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("gate_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_live_activations_activated_by_user_id", "live_activations", ["activated_by_user_id"])
    op.create_index("ix_live_activations_expires_at", "live_activations", ["expires_at"])
    op.create_index("ix_live_activations_revoked_at", "live_activations", ["revoked_at"])
    op.create_index("ix_live_activations_created_at", "live_activations", ["created_at"])
    op.create_index("ix_live_activations_expires_revoked", "live_activations", ["expires_at", "revoked_at"])

    # Written and committed before the request leaves the process, so an order
    # that reaches the exchange can never be one this system has no record of.
    op.create_table(
        "live_order_submissions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_order_id", sa.String(length=40), nullable=False),
        sa.Column(
            "paper_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "oms_order_id", UUID(as_uuid=True), sa.ForeignKey("oms_orders.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("approval_reference", sa.String(length=120), nullable=True),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("trading_symbol", sa.String(length=64), nullable=False),
        sa.Column("product", sa.String(length=10), nullable=False),
        sa.Column("price_type", sa.String(length=10), nullable=False),
        sa.Column("transaction_type", sa.String(length=5), nullable=False),
        sa.Column("retention", sa.String(length=10), nullable=False, server_default="DAY"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("trigger_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PREPARED"),
        sa.Column("broker_order_numbers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("response_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("failure_code", sa.String(length=20), nullable=True),
        sa.Column("failure_name", sa.String(length=60), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("resolution_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolution_detail", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_live_order_submissions_client_order_id", "live_order_submissions", ["client_order_id"]
    )
    op.create_index("ix_live_order_submissions_client_order_id", "live_order_submissions", ["client_order_id"])
    op.create_index("ix_live_order_submissions_paper_signal_id", "live_order_submissions", ["paper_signal_id"])
    op.create_index("ix_live_order_submissions_oms_order_id", "live_order_submissions", ["oms_order_id"])
    op.create_index("ix_live_order_submissions_approval_reference", "live_order_submissions", ["approval_reference"])
    op.create_index("ix_live_order_submissions_trading_symbol", "live_order_submissions", ["trading_symbol"])
    op.create_index("ix_live_order_submissions_status", "live_order_submissions", ["status"])
    op.create_index("ix_live_order_submissions_created_at", "live_order_submissions", ["created_at"])
    op.create_index("ix_live_order_submissions_status_created", "live_order_submissions", ["status", "created_at"])

    # Separate from trade_approval_intents, which is paper-only. A change made
    # for paper approvals must not be able to alter what reaches a broker.
    op.create_table(
        "live_order_approvals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("reference_id", sa.String(length=40), nullable=False),
        sa.Column(
            "paper_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("trading_symbol", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("product", sa.String(length=10), nullable=False),
        sa.Column("price_type", sa.String(length=10), nullable=False),
        sa.Column("transaction_type", sa.String(length=5), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("block_reason", sa.String(length=500), nullable=True),
        sa.Column("revalidation_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint("uq_live_order_approvals_reference", "live_order_approvals", ["reference_id"])
    op.create_index("ix_live_order_approvals_reference_id", "live_order_approvals", ["reference_id"])
    op.create_index("ix_live_order_approvals_paper_signal_id", "live_order_approvals", ["paper_signal_id"])
    op.create_index("ix_live_order_approvals_status", "live_order_approvals", ["status"])
    op.create_index("ix_live_order_approvals_expires_at", "live_order_approvals", ["expires_at"])
    op.create_index("ix_live_order_approvals_created_at", "live_order_approvals", ["created_at"])
    op.create_index("ix_live_order_approvals_status_created", "live_order_approvals", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("live_order_approvals")
    op.drop_table("live_order_submissions")
    op.drop_table("live_activations")
