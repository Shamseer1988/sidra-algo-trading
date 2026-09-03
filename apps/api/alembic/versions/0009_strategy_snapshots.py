"""add immutable strategy snapshots to scanner records

Revision ID: 0009_strategy_snapshots
Revises: 0008_scanner_evaluations
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_strategy_snapshots"
down_revision = "0008_scanner_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_signals", sa.Column("strategy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json"))
    )
    op.add_column(
        "scanner_evaluations",
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )


def downgrade() -> None:
    op.drop_column("scanner_evaluations", "strategy_snapshot")
    op.drop_column("paper_signals", "strategy_snapshot")
