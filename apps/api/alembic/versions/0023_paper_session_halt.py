"""Record the day a session's profit target or loss limit was reached.

The limits existed already but were recomputed on every check, which made them
filters rather than stops: session P&L counts open positions, so a winner
showing +2,200 could close at +800 and a day that had finished would start
trading again. This table is the latch.

One row per session date. Nothing to backfill — a halt is a decision taken at a
moment, and inventing one for a past session would put a verdict in the record
that nobody reached.

Revision ID: 0023_paper_session_halt
Revises: 0022_live_submission_broker
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0023_paper_session_halt"
down_revision = "0022_live_submission_broker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_session_halts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=60), nullable=False),
        sa.Column("session_pnl", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_date", name="uq_paper_session_halts_session_date"),
    )
    op.create_index("ix_paper_session_halts_session_date", "paper_session_halts", ["session_date"], unique=True)
    op.create_index("ix_paper_session_halts_created_at", "paper_session_halts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_paper_session_halts_created_at", table_name="paper_session_halts")
    op.drop_index("ix_paper_session_halts_session_date", table_name="paper_session_halts")
    op.drop_table("paper_session_halts")
