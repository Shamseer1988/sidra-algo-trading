"""Add the swing desk's two tables: corporate events, and its paper ledger.

Approved by Shamseer on 20 November 2026 as the one migration the swing desk's
paper trading needs.

``corporate_events`` holds splits, bonuses, dividends and results dates. Daily
candles are stored as the broker sent them and adjusted when read, from
confirmed rows here only.

``swing_paper_positions`` is the swing desk's own paper ledger. It is kept out
of ``paper_orders`` and ``paper_positions`` on purpose: those belong to the
intraday paper engine, which squares everything off in the afternoon and
whose fills the intraday desk's daily ceilings count.

**Safe to apply under a running deployment.** Two new tables; nothing that
exists is altered, so code that predates them neither notices nor cares.

Revision ID: 0030_swing_paper_ledger
Revises: 0029_trading_desks
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030_swing_paper_ledger"
down_revision = "0029_trading_desks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instrument_token", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=True),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("price_factor", sa.Numeric(12, 6), nullable=True),
        sa.Column("amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(60), nullable=False, server_default="manual"),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("instrument_token", "event_type", "ex_date", name="uq_corporate_events_token_type_date"),
        sa.CheckConstraint(
            "event_type IN ('SPLIT', 'BONUS', 'DIVIDEND', 'RESULTS')", name="ck_corporate_events_event_type"
        ),
        sa.CheckConstraint("price_factor IS NULL OR price_factor > 0", name="ck_corporate_events_price_factor"),
    )
    op.create_index("ix_corporate_events_instrument_token", "corporate_events", ["instrument_token"])
    op.create_index("ix_corporate_events_ex_date", "corporate_events", ["ex_date"])

    op.create_table(
        "swing_paper_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "paper_signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("instrument_token", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("sector", sa.String(60), nullable=True),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PLANNED"),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("entry_trigger", sa.Numeric(18, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("initial_stop", sa.Numeric(18, 4), nullable=False),
        sa.Column("stop_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("target_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("risk_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("max_holding_days", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("partial_exit_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partial_exit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("exit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_reason", sa.String(60), nullable=True),
        sa.Column("gross_pnl", sa.Numeric(18, 2), nullable=True),
        sa.Column("charges", sa.Numeric(18, 2), nullable=True),
        sa.Column("net_pnl", sa.Numeric(18, 2), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("paper_signal_id", name="uq_swing_paper_positions_signal"),
        sa.CheckConstraint(
            "status IN ('PLANNED', 'OPEN', 'CLOSED', 'CANCELLED')", name="ck_swing_paper_positions_status"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_swing_paper_positions_quantity"),
    )
    op.create_index("ix_swing_paper_positions_instrument_token", "swing_paper_positions", ["instrument_token"])
    op.create_index("ix_swing_paper_positions_status", "swing_paper_positions", ["status"])
    op.create_index("ix_swing_paper_positions_status_plan", "swing_paper_positions", ["status", "plan_date"])


def downgrade() -> None:
    op.drop_table("swing_paper_positions")
    op.drop_table("corporate_events")
