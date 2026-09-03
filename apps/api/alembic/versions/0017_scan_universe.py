"""add dynamic scan universe

Revision ID: 0017_scan_universe
Revises: 0016_live_readiness
"""

import sqlalchemy as sa

from alembic import op

revision = "0017_scan_universe"
down_revision = "0016_live_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_universe",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rejection_reason", sa.String(length=120), nullable=True),
        sa.Column("liquidity_score", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("volatility_score", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("gap_score", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("trend_score", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_date", "instrument_token", name="uq_scan_universe_session_instrument"),
    )
    op.create_index("ix_scan_universe_session_date", "scan_universe", ["session_date"])
    op.create_index("ix_scan_universe_instrument_token", "scan_universe", ["instrument_token"])
    op.create_index("ix_scan_universe_created_at", "scan_universe", ["created_at"])
    op.create_index("ix_scan_universe_session_selected", "scan_universe", ["session_date", "selected"])
    op.create_index("ix_scan_universe_session_rank", "scan_universe", ["session_date", "rank"])


def downgrade() -> None:
    op.drop_index("ix_scan_universe_session_rank", table_name="scan_universe")
    op.drop_index("ix_scan_universe_session_selected", table_name="scan_universe")
    op.drop_index("ix_scan_universe_created_at", table_name="scan_universe")
    op.drop_index("ix_scan_universe_instrument_token", table_name="scan_universe")
    op.drop_index("ix_scan_universe_session_date", table_name="scan_universe")
    op.drop_table("scan_universe")
