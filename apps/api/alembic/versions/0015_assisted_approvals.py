"""harden paper-only assisted trading approvals

Revision ID: 0015_assisted_approvals
Revises: 0014_shadow_mode
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_assisted_approvals"
down_revision = "0014_shadow_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trade_approval_intents", sa.Column("decided_at", sa.DateTime(timezone=True)))
    op.add_column("trade_approval_intents", sa.Column("risk_revalidated_at", sa.DateTime(timezone=True)))
    op.add_column("trade_approval_intents", sa.Column("submission_block_reason", sa.String(length=255)))


def downgrade() -> None:
    op.drop_column("trade_approval_intents", "submission_block_reason")
    op.drop_column("trade_approval_intents", "risk_revalidated_at")
    op.drop_column("trade_approval_intents", "decided_at")
