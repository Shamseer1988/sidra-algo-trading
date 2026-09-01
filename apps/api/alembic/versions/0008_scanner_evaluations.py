"""add auditable scanner evaluations

Revision ID: 0008_scanner_evaluations
Revises: 0007_paper_signal_outcomes
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_scanner_evaluations"
down_revision = "0007_paper_signal_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scanner_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evaluation_key", sa.String(length=220), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("candle_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_name", sa.String(length=120), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decision_state", sa.String(length=40), nullable=False),
        sa.Column("side", sa.String(length=10)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("failed_conditions", sa.JSON(), nullable=False),
        sa.Column("data_quality_state", sa.String(length=20), nullable=False, server_default="MISSING"),
        sa.Column("candle_close", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("candle_volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("indicator_snapshot", sa.JSON(), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("target_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("quantity", sa.Integer()),
        sa.Column("risk_amount", sa.Numeric(precision=18, scale=2)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("evaluation_key", name="uq_scanner_evaluation_key"),
    )
    op.create_index("ix_scanner_evaluations_evaluation_key", "scanner_evaluations", ["evaluation_key"], unique=True)
    op.create_index("ix_scanner_evaluations_instrument_token", "scanner_evaluations", ["instrument_token"])
    op.create_index("ix_scanner_evaluations_session_date", "scanner_evaluations", ["session_date"])
    op.create_index("ix_scanner_evaluations_candle_opened_at", "scanner_evaluations", ["candle_opened_at"])
    op.create_index("ix_scanner_evaluations_strategy_id", "scanner_evaluations", ["strategy_id"])
    op.create_index("ix_scanner_evaluations_status", "scanner_evaluations", ["status"])
    op.create_index("ix_scanner_evaluations_session_status", "scanner_evaluations", ["session_date", "status"])
    op.create_index("ix_scanner_evaluations_instrument_created", "scanner_evaluations", ["instrument_token", "created_at"])


def downgrade() -> None:
    op.drop_table("scanner_evaluations")
