"""add backtest parameter sweep

Revision ID: 0018_backtest_sweep
Revises: 0017_scan_universe
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_backtest_sweep"
down_revision = "0017_scan_universe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_sweeps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="COMPLETED"),
        sa.Column("strategy_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("validation_fraction", sa.Numeric(4, 3), nullable=False, server_default="0.35"),
        sa.Column("instrument_tokens", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("parameter_grid", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("combination_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("combinations", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("best_index", sa.Integer(), nullable=True),
        sa.Column("promoted_index", sa.Integer(), nullable=True),
        sa.Column("failure_detail", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_sweeps_created_by_user_id", "backtest_sweeps", ["created_by_user_id"])
    op.create_index("ix_backtest_sweeps_status", "backtest_sweeps", ["status"])
    op.create_index("ix_backtest_sweeps_strategy_id", "backtest_sweeps", ["strategy_id"])
    op.create_index("ix_backtest_sweeps_start_date", "backtest_sweeps", ["start_date"])
    op.create_index("ix_backtest_sweeps_end_date", "backtest_sweeps", ["end_date"])
    op.create_index("ix_backtest_sweeps_created_at", "backtest_sweeps", ["created_at"])
    op.create_index("ix_backtest_sweeps_created_status", "backtest_sweeps", ["created_at", "status"])


def downgrade() -> None:
    op.drop_index("ix_backtest_sweeps_created_status", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_created_at", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_end_date", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_start_date", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_strategy_id", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_status", table_name="backtest_sweeps")
    op.drop_index("ix_backtest_sweeps_created_by_user_id", table_name="backtest_sweeps")
    op.drop_table("backtest_sweeps")
