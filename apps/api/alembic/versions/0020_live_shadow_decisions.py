"""add live shadow decisions

Revision ID: 0020_live_shadow
Revises: 0019_live_recon_findings
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0020_live_shadow"
down_revision = "0019_live_recon_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per paper signal recording what the live path would have decided.
    # Nothing in this table results from a submission; Phase 3 submits nothing.
    op.create_table(
        "live_shadow_decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "oms_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oms_orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("translation_status", sa.String(length=20), nullable=False, server_default="UNRESOLVED"),
        sa.Column("trading_symbol", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=20), nullable=True),
        sa.Column("product", sa.String(length=10), nullable=True),
        sa.Column("price_type", sa.String(length=10), nullable=True),
        sa.Column("transaction_type", sa.String(length=5), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(18, 4), nullable=True),
        sa.Column("authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("failed_checks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("approval_mode", sa.String(length=30), nullable=False, server_default="DISABLED"),
        sa.Column("broker_margin_required", sa.Numeric(18, 2), nullable=True),
        sa.Column("broker_margin_available", sa.Numeric(18, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # One evaluation per signal, so a retried scanner pass cannot inflate the
    # authorisation statistics this table exists to produce.
    op.create_unique_constraint("uq_live_shadow_decisions_paper_signal", "live_shadow_decisions", ["paper_signal_id"])
    op.create_index("ix_live_shadow_decisions_paper_signal_id", "live_shadow_decisions", ["paper_signal_id"])
    op.create_index("ix_live_shadow_decisions_oms_order_id", "live_shadow_decisions", ["oms_order_id"])
    op.create_index("ix_live_shadow_decisions_instrument_token", "live_shadow_decisions", ["instrument_token"])
    op.create_index("ix_live_shadow_decisions_translation_status", "live_shadow_decisions", ["translation_status"])
    op.create_index("ix_live_shadow_decisions_authorized", "live_shadow_decisions", ["authorized"])
    op.create_index("ix_live_shadow_decisions_created_at", "live_shadow_decisions", ["created_at"])
    op.create_index(
        "ix_live_shadow_decisions_created_authorized", "live_shadow_decisions", ["created_at", "authorized"]
    )


def downgrade() -> None:
    op.drop_table("live_shadow_decisions")
