"""Record what the broker said about a day, beside our own figures rather than over them.

A broker's own P&L and charges are the authoritative money, and they arrive late:
Upstox reports charges aggregated over a date range and never per trade, so per-trade
costs in this system are always a local estimate and always will be. When the broker's
figures do arrive, the temptation is to correct the local rows with them. That would
destroy the only evidence of what this system believed at the time, which is exactly
what an audit needs and exactly what a reconciliation compares against.

So broker figures land here instead, and nothing reads back into paper_fills,
paper_positions or paper_orders. The History screen shows both numbers and says
whether they agree.

Append-only, with no unique key on (session_date, broker): a later fetch adds a row
rather than replacing one. Re-fetching a day is common — the broker's own figures
settle over hours — and a table that overwrote would lose the fact that they moved.

Revision ID: 0026_broker_day_snapshots
Revises: 0025_setting_revisions
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026_broker_day_snapshots"
down_revision = "0025_setting_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_day_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        # Nullable throughout: a broker endpoint that reports charges but not
        # turnover is normal, and a zero would read as "the broker said zero".
        sa.Column("realized_pnl", sa.Numeric(18, 4), nullable=True),
        sa.Column("charges", sa.Numeric(18, 4), nullable=True),
        sa.Column("turnover", sa.Numeric(18, 4), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_broker_day_snapshots_session_date", "broker_day_snapshots", ["session_date"])
    op.create_index("ix_broker_day_snapshots_broker", "broker_day_snapshots", ["broker"])
    op.create_index(
        "ix_broker_day_snapshots_date_broker_fetched",
        "broker_day_snapshots",
        ["session_date", "broker", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_day_snapshots_date_broker_fetched", table_name="broker_day_snapshots")
    op.drop_index("ix_broker_day_snapshots_broker", table_name="broker_day_snapshots")
    op.drop_index("ix_broker_day_snapshots_session_date", table_name="broker_day_snapshots")
    op.drop_table("broker_day_snapshots")
