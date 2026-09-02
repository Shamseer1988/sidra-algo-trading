"""add immutable live-readiness check history

Revision ID: 0016_live_readiness
Revises: 0015_assisted_approvals
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_live_readiness"
down_revision = "0015_assisted_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_readiness_checks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("overall_ready", sa.Boolean(), nullable=False),
        sa.Column("gate_snapshot", sa.JSON(), nullable=False),
        sa.Column("checked_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["checked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_live_readiness_checks_created_status", "live_readiness_checks", ["created_at", "status"])
    op.create_index("ix_live_readiness_checks_status", "live_readiness_checks", ["status"])
    op.create_index("ix_live_readiness_checks_created_at", "live_readiness_checks", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_live_readiness_checks_created_at", table_name="live_readiness_checks")
    op.drop_index("ix_live_readiness_checks_status", table_name="live_readiness_checks")
    op.drop_index("ix_live_readiness_checks_created_status", table_name="live_readiness_checks")
    op.drop_table("live_readiness_checks")
