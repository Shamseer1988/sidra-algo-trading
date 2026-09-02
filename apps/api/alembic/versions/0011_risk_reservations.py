"""add transactional paper risk reservations

Revision ID: 0011_risk_reservations
Revises: 0010_paper_execution
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_risk_reservations"
down_revision = "0010_paper_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("paper_signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("instrument_token", sa.String(length=64), nullable=False),
        sa.Column("risk_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("decision_reason", sa.String(length=255), nullable=False, server_default="Paper risk reserved"),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("paper_signal_id", name="uq_risk_reservations_paper_signal_id"),
    )
    op.create_index("ix_risk_reservations_paper_signal_id", "risk_reservations", ["paper_signal_id"])
    op.create_index("ix_risk_reservations_session_date", "risk_reservations", ["session_date"])
    op.create_index("ix_risk_reservations_instrument_token", "risk_reservations", ["instrument_token"])
    op.create_index("ix_risk_reservations_status", "risk_reservations", ["status"])
    op.create_index("ix_risk_reservations_session_status", "risk_reservations", ["session_date", "status"])


def downgrade() -> None:
    op.drop_table("risk_reservations")
