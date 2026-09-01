"""add paper signal journal outcomes

Revision ID: 0007_paper_signal_outcomes
Revises: 0006_upstox_oauth_and_instruments
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_paper_signal_outcomes"
down_revision = "0006_upstox_oauth_and_instruments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_signal_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("exit_price", sa.Numeric(precision=18, scale=4)),
        sa.Column("realized_r", sa.Numeric(precision=12, scale=4)),
        sa.Column("mfe_r", sa.Numeric(precision=12, scale=4), nullable=False, server_default="0"),
        sa.Column("mae_r", sa.Numeric(precision=12, scale=4), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("paper_signal_id", name="uq_paper_signal_outcome_signal"),
    )
    op.create_index("ix_paper_signal_outcomes_paper_signal_id", "paper_signal_outcomes", ["paper_signal_id"])
    op.create_index("ix_paper_signal_outcomes_status", "paper_signal_outcomes", ["status"])


def downgrade() -> None:
    op.drop_table("paper_signal_outcomes")
