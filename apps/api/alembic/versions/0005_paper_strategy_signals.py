"""add paper-only scanner signal records

Revision ID: 0005_paper_strategy_signals
Revises: 0004_market_calculations
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_paper_strategy_signals"
down_revision = "0004_market_calculations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_key", sa.String(length=180), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("candle_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_version", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PAPER_RECORDED"),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("target_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("risk_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("indicator_snapshot", sa.JSON(), nullable=False),
        sa.Column("alert_detail", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("signal_key", name="uq_paper_signal_key"),
    )
    op.create_index("ix_paper_signals_signal_key", "paper_signals", ["signal_key"])
    op.create_index("ix_paper_signals_instrument_token", "paper_signals", ["instrument_token"])
    op.create_index("ix_paper_signals_session_date", "paper_signals", ["session_date"])
    op.create_index("ix_paper_signals_candle_opened_at", "paper_signals", ["candle_opened_at"])
    op.create_index("ix_paper_signals_side", "paper_signals", ["side"])
    op.create_index("ix_paper_signals_status", "paper_signals", ["status"])
    op.create_index("ix_paper_signals_session_status", "paper_signals", ["session_date", "status"])


def downgrade() -> None:
    op.drop_table("paper_signals")
