"""add structured findings to execution reconciliations

Revision ID: 0019_live_recon_findings
Revises: 0018_backtest_sweep
"""

import sqlalchemy as sa

from alembic import op

revision = "0019_live_recon_findings"
down_revision = "0018_backtest_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A live reconciliation that blocks trading has to say exactly what it found:
    # which broker order was untracked, which position was unexplained. The existing
    # 255-character detail column can hold a summary but not the evidence, and an
    # operator deciding whether to override a block needs the evidence.
    op.add_column(
        "execution_reconciliations",
        sa.Column("findings", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "execution_reconciliations",
        sa.Column("safe_to_trade", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_execution_reconciliations_safe_to_trade",
        "execution_reconciliations",
        ["safe_to_trade"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_reconciliations_safe_to_trade", table_name="execution_reconciliations")
    op.drop_column("execution_reconciliations", "safe_to_trade")
    op.drop_column("execution_reconciliations", "findings")
