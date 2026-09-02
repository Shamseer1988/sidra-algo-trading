"""add zero-submission shadow order comparisons

Revision ID: 0014_shadow_mode
Revises: 0013_oms_core
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_shadow_mode"
down_revision = "0013_oms_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("oms_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("oms_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("intended_quantity", sa.Integer(), nullable=False),
        sa.Column("intended_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("comparison_status", sa.String(length=30), nullable=False, server_default="AWAITING_PAPER_FILL"),
        sa.Column("paper_fill_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("price_delta", sa.Numeric(precision=18, scale=4)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("compared_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("oms_order_id", name="uq_shadow_orders_oms_order_id"),
    )
    op.create_index("ix_shadow_orders_oms_order_id", "shadow_orders", ["oms_order_id"])
    op.create_index("ix_shadow_orders_instrument_token", "shadow_orders", ["instrument_token"])
    op.create_index("ix_shadow_orders_comparison_status", "shadow_orders", ["comparison_status"])
    op.create_index("ix_shadow_orders_status_updated", "shadow_orders", ["comparison_status", "updated_at"])


def downgrade() -> None:
    op.drop_table("shadow_orders")
